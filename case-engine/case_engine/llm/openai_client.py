"""OpenAI Chat-Completions adapter (tool_choice='required' enforcement).

The adapter is deliberately thin and dependency-lazy: ``openai`` is
imported only when a client is actually constructed, so the engine core
(install with ``pip install case-engine``) never requires provider SDKs.
Transport/SDK exceptions are wrapped into :class:`LLMAdapterError`.

Invariants
----------
* Implements the exact
  :meth:`~case_engine.llm.base.BaseLLMClient.generate_tool_call`
  contract: forces the tool via ``tool_choice="required"``, extracts
  ``message.tool_calls[0]`` and returns the arguments **unvalidated**
  (the enforcement gate owns schema validation).
* Provider-shaped on the wire, semantically identical in result: the
  returned :class:`~case_engine.llm.base.ToolCallResult` is the same
  shape every other adapter produces.
* SDK exceptions never escape unwrapped; "model answered without a tool
  call" is :class:`LLMResponseError` (reflexion-routed), everything
  transport/JSON-related is :class:`LLMAdapterError`.
* Never logs API keys, prompts or completion text — only lengths,
  model id, tool name, latency, finish reason and token counts.

Contents
--------
- :class:`OpenAIClient` — official OpenAI Chat-Completions adapter.
- :class:`OpenAICompatibleClient` — any OpenAI-compatible endpoint
  (vLLM, LM Studio, gateways); base of the Ollama/vLLM adapters.

Cross-references: ``docs/LLM_PROVIDERS.md`` §2 (adapter table) and §3
(zero-trust note: provider-side enforcement is an optimization, the
gate re-validates everything).
"""

from __future__ import annotations

import json
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

__all__ = ["OpenAIClient", "OpenAICompatibleClient"]

log = get_logger(__name__)


class OpenAIClient(BaseLLMClient):
    """Adapter for the official OpenAI Chat-Completions API.

    Args:
        model: Model id (e.g. ``"gpt-4o"``).
        api_key: API key; falls back to the ``OPENAI_API_KEY`` env var.
            Never logged.
        base_url: Optional custom endpoint (Azure proxy, gateway...).
        temperature: Sampling temperature (0.0 = deterministic-ish).
        timeout_s: Request timeout in seconds.
        retry_attempts: Total tries per request for *transient transport*
            failures (408/429/5xx, connection, timeout — G8).  ``1``
            disables retrying; the default ``3`` applies exponential
            backoff with jitter and honours numeric ``Retry-After``
            hints.  Content failures (no tool call) are never retried —
            those belong to the pipeline's reflexion loop.
        retry_initial_backoff_s: Sleep before the first retry.
        retry_max_backoff_s: Ceiling for any single sleep.
        client: Pre-built SDK client (injection point for tests/proxies).

    Raises:
        LLMProviderUnavailableError: When the ``openai`` package is not
            installed (lazy import fails).  The message carries the
            exact install command; logged at ERROR by construction
            sites via the adapter-failure path.

    Example:
        >>> client = OpenAIClient(model="gpt-4o", api_key="sk-...")
        >>> client.name
        'openai'
    """

    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
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
            base_url: Optional custom endpoint override.
            temperature: Sampling temperature.
            timeout_s: Request timeout in seconds.
            retry_attempts: Transport retry budget (G8; ``1`` = disabled).
            retry_initial_backoff_s: First-retry sleep seconds.
            retry_max_backoff_s: Per-sleep ceiling (Retry-After clamped).
            client: Pre-built SDK client; when given, the lazy import is
                skipped entirely (test/proxy injection point).

        Raises:
            LLMProviderUnavailableError: When the SDK client must be
                constructed and the ``openai`` package is missing —
                logged at ERROR (with the ImportError chained) before
                raising.
            ValueError: When the retry policy parameters are invalid
                (negative durations / ``retry_attempts < 1``).
        """
        self._model = model
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._retry_policy = RetryPolicy(
            max_attempts=retry_attempts,
            initial_backoff_s=retry_initial_backoff_s,
            max_backoff_s=retry_max_backoff_s,
        )
        if client is not None:
            self._client = client
        else:
            try:
                import openai
            except ImportError as exc:
                log.error(
                    "openai SDK not installed (lazy import failed)",
                    extra={"provider": self.name, "model": model},
                    exc_info=True,
                )
                raise LLMProviderUnavailableError(
                    "The 'openai' package is not installed. "
                    "Install with: pip install 'case-engine[openai]'"
                ) from exc
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
        log.info("OpenAIClient ready", extra={"model": model})

    # -- public API --------------------------------------------------------
    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        """Force one schema-gated tool call via Chat-Completions.

        Dispatch and completion are logged at INFO (provider, model,
        latency, tool, finish reason, token counts when the response
        carries usage data); the request/response bodies never are.

        Args:
            request: Prepared request (OpenAI-shaped tool specs pass through
                unchanged; ``tool_choice='required'`` forbids free text).

        Returns:
            The first tool call of the response, with arguments parsed
            from JSON and left unvalidated for the enforcement gate.

        Raises:
            LLMResponseError: Model answered without tool calls — logged
                WARNING (model degradation; reflexion-routed).
            LLMAdapterError: Transport/SDK failures (logged ERROR with
                ``exc_info=True``) or non-JSON tool-call arguments
                (logged ERROR with the tool name and traceback).
        """
        requested_tools = [
            spec.get("function", {}).get("name") or spec.get("name", "")
            for spec in request.tool_specs
            if isinstance(spec, dict)
        ]
        log.debug(
            "OpenAI request prepared",
            extra={
                "provider": self.name, "model": self._model,
                "tools": requested_tools,
                "session_id": request.metadata.get("session_id", "-"),
                "system_prompt_chars": len(request.system_prompt),
                "user_prompt_chars": len(request.user_prompt),
                "max_tokens": request.max_tokens,
            },
        )
        kwargs = self.build_request_kwargs(request)
        log.info(
            "LLM request dispatched",
            extra={"provider": self.name, "model": self._model,
                   "tools": requested_tools},
        )
        started = time.perf_counter()
        try:
            # G8: transient transport failures (429/5xx/timeout/connection)
            # are retried with exponential backoff + jitter inside this
            # call; the final failure still raises the original SDK
            # exception so the wrapping below stays the single boundary.
            response = call_with_retries(
                lambda: self._client.chat.completions.create(**kwargs),
                policy=self._retry_policy,
                provider=self.name,
                what="chat.completions",
            )
        except Exception as exc:
            log.error(
                "OpenAI request failed",
                extra={"provider": self.name, "model": self._model,
                       "retry_attempts": self._retry_policy.max_attempts},
                exc_info=True,
            )
            raise LLMProviderUnavailableError(
                f"OpenAI request failed after up to "
                f"{self._retry_policy.max_attempts} attempt(s): {exc}",
                provider=self.name, model=self._model,
            ) from exc
        latency = (time.perf_counter() - started) * 1000.0

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        raw_content = getattr(message, "content", None) or ""
        if not tool_calls:
            log.warning(
                "model answered without a tool call (degradation -> reflexion)",
                extra={"provider": self.name, "model": self._model,
                       "raw_chars": len(raw_content)},
            )
            raise LLMResponseError(
                "Model responded without a tool call despite tool_choice='required'",
                provider=self.name, model=self._model, raw=raw_content[:200],
            )
        call = tool_calls[0]
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            log.error(
                "tool-call arguments are not valid JSON",
                extra={"provider": self.name, "model": self._model,
                       "tool": call.function.name},
                exc_info=True,
            )
            raise LLMAdapterError(
                f"Tool-call arguments are not valid JSON: {exc}",
                provider=self.name, model=self._model,
            ) from exc

        usage = getattr(response, "usage", None)
        usage_dict: dict[str, int] | None = None
        if usage is not None:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            usage_dict = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        log.info(
            "LLM request completed",
            extra={
                "provider": self.name, "model": self._model,
                "tool": call.function.name,
                "latency_ms": round(latency, 3),
                "finish_reason": getattr(response.choices[0], "finish_reason", "") or "",
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        )
        return ToolCallResult(
            tool_name=call.function.name,
            arguments=arguments,
            provider=self.name,
            latency_ms=round(latency, 3),
            raw_content=raw_content,
            finish_reason=getattr(response.choices[0], "finish_reason", "") or "",
            usage=usage_dict,
        )

    @staticmethod
    def build_request_kwargs(request: ToolCallRequest, model: str = "gpt-4o",
                             temperature: float = 0.0) -> dict[str, Any]:
        """Build the Chat-Completions kwargs (pure; unit-testable without SDK).

        The mapping is intentionally loss-free: OpenAI-shaped specs and
        the ``tool_choice`` value pass through unchanged, so the request
        shape is exactly what the Chat-Completions API expects.

        Args:
            request: Prepared request.
            model: Model id override.
            temperature: Sampling temperature override.

        Returns:
            Dict ready for ``client.chat.completions.create(**kwargs)``.
        """
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "tools": request.tool_specs,
            "tool_choice": request.tool_choice if request.tool_choice != "required" else "required",
            "temperature": temperature,
            "max_tokens": request.max_tokens,
        }
        # Metadata only — prompt contents are never logged.
        log.debug(
            "OpenAI kwargs built",
            extra={"model": model, "tools": len(request.tool_specs),
                   "temperature": temperature, "max_tokens": request.max_tokens},
        )
        return kwargs


class OpenAICompatibleClient(OpenAIClient):
    """Adapter for any OpenAI-compatible endpoint (vLLM, LM Studio, gateways).

    Inherits the full Chat-Completions behaviour (forced tool call,
    ``tool_calls[0]`` extraction, error normalization) and only retargets
    the endpoint — this is the base class of the Ollama and vLLM
    adapters in :mod:`case_engine.llm.ollama_client`.

    Example:
        >>> client = OpenAICompatibleClient(
        ...     base_url="http://localhost:8000/v1", model="Qwen2.5-7B-Instruct")
    """

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = "local",
        temperature: float = 0.0,
        timeout_s: float = 120.0,
        retry_attempts: int = 3,
        retry_initial_backoff_s: float = 0.5,
        retry_max_backoff_s: float = 8.0,
        client: Any | None = None,
    ) -> None:
        """Delegate to :class:`OpenAIClient` with a custom endpoint.

        Args:
            base_url: e.g. ``"http://localhost:8000/v1"`` (vLLM).
            model: Model id served by the endpoint.
            api_key: Dummy key for local servers.  Never logged.
            temperature: Sampling temperature.
            timeout_s: Request timeout.
            retry_attempts: Transport retry budget (G8; inherited loop).
            retry_initial_backoff_s: First-retry sleep seconds.
            retry_max_backoff_s: Per-sleep ceiling.
            client: Pre-built SDK client (test injection).

        Raises:
            LLMProviderUnavailableError: Inherited from
                :class:`OpenAIClient` when the ``openai`` package is
                missing and no client was injected.
        """
        super().__init__(
            model=model, api_key=api_key, base_url=base_url,
            temperature=temperature, timeout_s=timeout_s,
            retry_attempts=retry_attempts,
            retry_initial_backoff_s=retry_initial_backoff_s,
            retry_max_backoff_s=retry_max_backoff_s,
            client=client,
        )
        self.name = "openai_compatible"
        log.debug(
            "openai-compatible adapter configured",
            extra={"base_url": base_url, "model": model},
        )
