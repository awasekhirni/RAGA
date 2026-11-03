"""Anthropic Messages-API adapter (forced tool use).

Claude's tool-use contract differs from OpenAI's: tools carry
``input_schema`` and forcing is expressed via ``tool_choice``.  When the
engine requests exactly one tool (the norm — one mental model per call),
the adapter forces THAT tool, giving the strongest possible compliance;
with multiple tools it falls back to ``{"type": "any"}``.

Invariants
----------
* Implements the exact
  :meth:`~case_engine.llm.base.BaseLLMClient.generate_tool_call`
  contract: forces the tool, extracts the first ``tool_use`` block and
  returns its input **unvalidated** (the enforcement gate owns schema
  validation).
* Tool-call payloads are provider-shaped on the wire (``tool_use``
  blocks with ``input_schema``) but semantically identical once
  extracted — the same :class:`~case_engine.llm.base.ToolCallResult`
  shape every other adapter returns.
* SDK exceptions never escape unwrapped; "model answered without a
  ``tool_use`` block" is :class:`LLMResponseError` (reflexion-routed),
  everything transport-related is :class:`LLMAdapterError`.
* Never logs API keys, prompts or completion text — only lengths,
  model id, tool name, latency, stop reason and token counts.

Contents
--------
- :class:`AnthropicClient` — Messages-API adapter with per-tool forcing.

Cross-references: ``docs/LLM_PROVIDERS.md`` §2 (adapter table and the
single-tool vs. multi-tool forcing note) and §3 (zero-trust note).
"""

from __future__ import annotations

import time
from typing import Any

from case_engine.exceptions import (
    LLMAdapterError,
    LLMProviderUnavailableError,
    LLMResponseError,
)
from case_engine.llm.base import BaseLLMClient, ToolCallRequest, ToolCallResult
from case_engine.llm.retry import RetryPolicy, call_with_retries
from case_engine.logging_setup import get_logger

__all__ = ["AnthropicClient"]

log = get_logger(__name__)


class AnthropicClient(BaseLLMClient):
    """Adapter for the Anthropic Messages API.

    Args:
        model: Model id (e.g. ``"claude-sonnet-4-20250514"``).
        api_key: API key; falls back to ``ANTHROPIC_API_KEY``.
            Never logged.
        max_tokens: Default output ceiling (overridden per request).
        temperature: Sampling temperature.
        timeout_s: Request timeout.
        retry_attempts: Total tries per request for *transient transport*
            failures (408/429/5xx ("overloaded"), connection, timeout —
            G8).  ``1`` disables retrying; the default ``3`` applies
            exponential backoff with jitter and honours numeric
            ``Retry-After`` hints.  Content failures (no tool_use block)
            are never retried — reflexion owns those.
        retry_initial_backoff_s: Sleep before the first retry.
        retry_max_backoff_s: Ceiling for any single sleep.
        client: Pre-built SDK client (injection point for tests).

    Raises:
        LLMProviderUnavailableError: When the ``anthropic`` package is
            not installed (lazy import fails).  The message carries the
            exact install command.

    Example:
        >>> client = AnthropicClient(model="claude-sonnet-4-20250514", api_key="sk-...")
        >>> client.name
        'anthropic'
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
        retry_attempts: int = 3,
        retry_initial_backoff_s: float = 0.5,
        retry_max_backoff_s: float = 8.0,
        client: Any | None = None,
    ) -> None:
        """Store settings; lazily construct the SDK client when not injected.

        Args:
            model: Model id sent with every request.
            api_key: API key (or ``None`` to let the SDK read the env
                var).  Never logged.
            max_tokens: Default output ceiling; each request uses
                ``min(self._max_tokens, request.max_tokens)``.
            temperature: Sampling temperature.
            timeout_s: Request timeout in seconds.
            retry_attempts: Transport retry budget (G8; ``1`` = disabled).
            retry_initial_backoff_s: First-retry sleep seconds.
            retry_max_backoff_s: Per-sleep ceiling (Retry-After clamped).
            client: Pre-built SDK client; when given, the lazy import is
                skipped entirely (test injection point).

        Raises:
            LLMProviderUnavailableError: When the SDK client must be
                constructed and the ``anthropic`` package is missing —
                logged at ERROR (with the ImportError chained) before
                raising.
            ValueError: When the retry policy parameters are invalid.
        """
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._retry_policy = RetryPolicy(
            max_attempts=retry_attempts,
            initial_backoff_s=retry_initial_backoff_s,
            max_backoff_s=retry_max_backoff_s,
        )
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as exc:
                log.error(
                    "anthropic SDK not installed (lazy import failed)",
                    extra={"provider": self.name, "model": model},
                    exc_info=True,
                )
                raise LLMProviderUnavailableError(
                    "The 'anthropic' package is not installed. "
                    "Install with: pip install 'case-engine[anthropic]'"
                ) from exc
            self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
        log.info("AnthropicClient ready", extra={"model": model})

    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        """Force one schema-gated tool call via the Messages API.

        Dispatch and completion are logged at INFO (provider, model,
        latency, tool, stop reason, input/output token counts when the
        response carries usage data); the request/response bodies never
        are.

        Args:
            request: Prepared request; OpenAI-shaped specs are converted
                by :meth:`_convert_tools`.

        Returns:
            The first ``tool_use`` block of the response, its ``input``
            dict passed through unvalidated for the enforcement gate.

        Raises:
            LLMResponseError: Model answered without a tool-use block —
                logged WARNING (model degradation; reflexion-routed).
            LLMAdapterError: Transport/SDK failures — logged ERROR with
                ``exc_info=True`` before raising.
        """
        log.debug(
            "Anthropic request prepared",
            extra={
                "provider": self.name, "model": self._model,
                "tools": len(request.tool_specs),
                "session_id": request.metadata.get("session_id", "-"),
                "system_prompt_chars": len(request.system_prompt),
                "user_prompt_chars": len(request.user_prompt),
                "max_tokens": request.max_tokens,
            },
        )
        kwargs = self.build_request_kwargs(
            request, model=self._model, max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        log.info(
            "LLM request dispatched",
            extra={"provider": self.name, "model": self._model,
                   "tools": len(request.tool_specs)},
        )
        started = time.perf_counter()
        try:
            # G8: transient transport failures retried with exponential
            # backoff + jitter; the final SDK exception still propagates
            # to the single wrapping boundary below.
            response = call_with_retries(
                lambda: self._client.messages.create(**kwargs),
                policy=self._retry_policy,
                provider=self.name,
                what="messages.create",
            )
        except Exception as exc:
            log.error(
                "Anthropic request failed",
                extra={"provider": self.name, "model": self._model,
                       "retry_attempts": self._retry_policy.max_attempts},
                exc_info=True,
            )
            raise LLMProviderUnavailableError(
                f"Anthropic request failed after up to "
                f"{self._retry_policy.max_attempts} attempt(s): {exc}",
                provider=self.name, model=self._model,
            ) from exc
        latency = (time.perf_counter() - started) * 1000.0

        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        raw_text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        )
        if not tool_uses:
            log.warning(
                "model answered without a tool_use block (degradation -> reflexion)",
                extra={"provider": self.name, "model": self._model,
                       "raw_chars": len(raw_text)},
            )
            raise LLMResponseError(
                "Model responded without a tool_use block despite forced tool choice",
                provider=self.name, model=self._model, raw=raw_text[:200],
            )
        call = tool_uses[0]
        usage = getattr(response, "usage", None)
        usage_dict: dict[str, int] | None = None
        if usage is not None:
            prompt = int(getattr(usage, "input_tokens", 0) or 0)
            completion = int(getattr(usage, "output_tokens", 0) or 0)
            usage_dict = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        log.info(
            "LLM request completed",
            extra={
                "provider": self.name, "model": self._model,
                "tool": call.name,
                "latency_ms": round(latency, 3),
                "finish_reason": getattr(response, "stop_reason", "") or "",
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
        )
        return ToolCallResult(
            tool_name=call.name,
            arguments=dict(call.input or {}),
            provider=self.name,
            latency_ms=round(latency, 3),
            raw_content=raw_text,
            finish_reason=getattr(response, "stop_reason", "") or "",
            usage=usage_dict,
        )

    @staticmethod
    def _convert_tools(openai_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-shaped specs to Anthropic tool dicts.

        The semantic mapping is loss-free: ``name`` stays ``name``,
        ``description`` stays ``description`` and the OpenAI
        ``parameters`` JSON Schema becomes the Anthropic
        ``input_schema`` (both are JSON Schema dialects).

        Args:
            openai_specs: ``[{"type": "function", "function": {...}}, ...]``.

        Returns:
            ``[{"name", "description", "input_schema"}, ...]``.

        Raises:
            LLMAdapterError: If a spec is not OpenAI-shaped (missing or
                empty ``function`` object, or no ``name``) — a wiring
                error; logged at ERROR before raising.
        """
        converted: list[dict[str, Any]] = []
        for spec in openai_specs:
            fn = spec.get("function") if isinstance(spec, dict) else None
            if not fn or "name" not in fn:
                log.error(
                    "non-OpenAI-shaped tool spec received",
                    extra={"spec_keys": sorted(spec) if isinstance(spec, dict) else None},
                )
                raise LLMAdapterError("AnthropicClient received a non-OpenAI-shaped tool spec")
            converted.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object"}),
            })
        log.debug("tool specs converted to Anthropic shape", extra={"count": len(converted)})
        return converted

    @staticmethod
    def build_request_kwargs(
        request: ToolCallRequest,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Build Messages-API kwargs (pure; unit-testable without SDK).

        Forcing strategy: a single-tool request is pinned to that exact
        tool (``{"type": "tool", "name": ...}``) — the strongest
        compliance available; multi-tool requests fall back to
        ``{"type": "any"}`` (some tool must still be used).

        Args:
            request: Prepared request.
            model: Model id override.
            max_tokens: Output ceiling override (further clamped by
                ``request.max_tokens``).
            temperature: Sampling temperature override.

        Returns:
            Dict ready for ``client.messages.create(**kwargs)``.

        Raises:
            LLMAdapterError: Propagated from :meth:`_convert_tools` when
                a spec is not OpenAI-shaped (logged there).
        """
        tools = AnthropicClient._convert_tools(request.tool_specs)
        if len(tools) == 1:
            tool_choice: dict[str, str] = {"type": "tool", "name": tools[0]["name"]}
        else:
            tool_choice = {"type": "any"}
        kwargs = {
            "model": model,
            "max_tokens": min(max_tokens, request.max_tokens),
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
        }
        # Metadata only — prompt contents are never logged.
        log.debug(
            "Anthropic kwargs built",
            extra={"model": model, "tools": len(tools),
                   "tool_choice": tool_choice.get("type"),
                   "max_tokens": kwargs["max_tokens"]},
        )
        return kwargs
