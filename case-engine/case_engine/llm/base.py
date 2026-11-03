"""LLM adapter base contracts — the provider-agnostic tool-call protocol.

The engine **only** ever communicates with models through schema-gated
tool calls (Strategy 1 of the architecture): free-text generation is not
part of the contract.  Every provider adapter — OpenAI, Anthropic,
Ollama, vLLM and the deterministic :class:`~case_engine.llm.mock_client.MockLLMClient`
— implements the same one-method protocol defined by
:meth:`BaseLLMClient.generate_tool_call`, so the pipeline, the reflexion
loop and the enforcement gate are entirely provider-agnostic.

Invariants
----------
* All adapters implement the same protocol: one :class:`ToolCallRequest`
  in, one :class:`ToolCallResult` out.
* Tool-call payloads are provider-shaped on the wire (OpenAI
  ``function`` objects, Anthropic ``tool_use`` blocks) but semantically
  identical once extracted: ``tool_name`` + raw ``arguments`` dict.
* Arguments are returned **unvalidated** — the enforcement gate
  (``case_engine.enforcement.gate``) is the single source of truth for
  schema validation; adapters must never validate.
* SDK exceptions never leak: adapters normalize every failure into the
  :class:`~case_engine.exceptions.LLMAdapterError` family so the
  pipeline can route it into the reflexion loop instead of crashing.
* Credentials and full prompt/completion payloads are never logged;
  adapters log model, tool name, prompt lengths, latency and token
  counts instead.

Contents
--------
- :class:`ToolCallRequest` — one schema-gated generation request.
- :class:`ToolCallResult` — one tool call extracted from a response.
- :class:`BaseLLMClient` — abstract adapter + shared helpers.

Cross-references: ``docs/LLM_PROVIDERS.md`` (adapter table, forcing
mechanisms per provider, custom-provider guide, zero-trust note) and
``docs/AUDIT_LEDGER.md`` (how session/node metadata flows into the
audit trail).
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from case_engine.exceptions import LLMAdapterError
from case_engine.logging_setup import get_logger

__all__ = ["ToolCallRequest", "ToolCallResult", "BaseLLMClient"]

log = get_logger(__name__)


@dataclass
class ToolCallRequest:
    """One schema-gated generation request.

    The engine builds exactly one request per reasoning attempt; a
    single-tool ``tool_specs`` list is the norm (one mental model per
    call).  Adapters translate the OpenAI-shaped specs to their provider
    shape and translate ``tool_choice="required"`` into the strongest
    native forcing mechanism available.

    Attributes:
        system_prompt: Node-specific system instructions.
        user_prompt: User query + reflexion feedback block.
        tool_specs: OpenAI-shaped tool specs (single-tool lists are the
            norm; adapters convert to provider shapes).
        tool_choice: ``"required"`` semantics — the adapter MUST force a
            tool call.  Provider-specific values allowed.
        max_tokens: Token ceiling from ``guardrails.max_reasoning_tokens``.
        metadata: Free-form tags copied into logs (session_id, node...);
            never forwarded to the provider API.

    Example:
        >>> req = ToolCallRequest(system_prompt="s", user_prompt="u",
        ...     tool_specs=[{"type": "function",
        ...                  "function": {"name": "MyTool", "parameters": {}}}])
        >>> req.primary_tool_name
        'MyTool'
    """

    system_prompt: str
    user_prompt: str
    tool_specs: list[dict[str, Any]]
    tool_choice: Any = "required"
    max_tokens: int = 4096
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_tool_name(self) -> str:
        """Return the name of the first (usually only) requested tool.

        The engine sends one tool per request (one mental model per
        call), so the first spec is the tool the adapter must force.

        Returns:
            The tool name: the OpenAI-shaped ``function.name`` when
            present, else the flat ``name`` key (Anthropic-shaped
            specs), else ``""``.

        Raises:
            LLMAdapterError: If the request carries no tool specs — a
                programming error, since an adapter cannot force a tool
                it was never given.  Logged at ERROR (with the session
                metadata) before raising.

        Example:
            >>> empty = ToolCallRequest(system_prompt="s", user_prompt="u",
            ...                         tool_specs=[])
            >>> empty.primary_tool_name
            Traceback (most recent call last):
            ...
            case_engine.exceptions.LLMAdapterError: ToolCallRequest carries no tool specs
        """
        if not self.tool_specs:
            log.error(
                "ToolCallRequest carries no tool specs",
                extra={"session_id": self.metadata.get("session_id", "-")},
            )
            raise LLMAdapterError("ToolCallRequest carries no tool specs")
        spec = self.tool_specs[0]
        name = spec.get("function", {}).get("name") or spec.get("name", "")
        log.debug("primary tool resolved", extra={"tool": name})
        return name


@dataclass
class ToolCallResult:
    """One tool call extracted from a provider response.

    This is the normalized, provider-independent answer shape: whatever
    the wire format was (OpenAI ``tool_calls[0]``, Anthropic ``tool_use``
    block, mock script), every adapter returns exactly this.

    Attributes:
        tool_name: Tool the model invoked — expected to match one of the
            requested spec names (the gate rejects unknown tools).
        arguments: Raw argument dict (unvalidated — the gate validates).
        provider: Adapter name (``"openai"`` | ``"anthropic"`` | ...).
        latency_ms: Wall-clock request duration.
        raw_content: Any accompanying free text (for audit/debug only;
            the pipeline never interprets it).
        finish_reason: Provider finish reason, when available.
        usage: Provider-reported token usage as a JSON-safe dict with the
            normalized keys ``prompt_tokens``, ``completion_tokens`` and
            ``total_tokens`` (G7); ``None`` when the provider did not
            report usage.  Counts only — never content.

    Example:
        >>> ToolCallResult(tool_name="MyTool", arguments={"a": 1},
        ...                provider="mock").tool_name
        'MyTool'
    """

    tool_name: str
    arguments: dict[str, Any]
    provider: str
    latency_ms: float = 0.0
    raw_content: str = ""
    finish_reason: str = ""
    usage: dict[str, int] | None = None


class BaseLLMClient(ABC):
    """Abstract provider adapter — the contract every backend honours.

    Subclasses must be defensive: SDK exceptions are wrapped into
    :class:`LLMAdapterError` subclasses so the pipeline can route them
    to the reflexion loop instead of crashing.

    Attributes:
        name: Provider key used in logs and in
            ``ToolCallResult.provider``.

    Implementer's checklist (see ``docs/LLM_PROVIDERS.md``):

    1. Force the tool with the provider's native mechanism when
       available (``tool_choice="required"`` for OpenAI-shaped APIs,
       ``{"type": "tool", "name": ...}`` for Anthropic).
    2. Never parse the model's free text — only its tool-call arguments.
    3. Return arguments raw and unvalidated; the gate owns validation.
    4. Normalize transport/auth/SDK failures into
       :class:`LLMAdapterError` and "model produced no usable tool call"
       into :class:`LLMResponseError` (both are reflexion-routed, never
       fatal to the process).
    5. Never log API keys or full prompt/completion payloads; log
       lengths, model id, tool name, latency and token counts instead.
    """

    #: Provider key used in logs and results.
    name: str = "base"

    @abstractmethod
    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        """Force one schema-gated tool call.

        The single communication primitive between the engine and a
        model.  Adapters MUST honour this exact contract:

        * **Arguments** — ``request`` carries the system prompt, the
          user prompt (query + reflexion feedback), OpenAI-shaped
          ``tool_specs``, a ``tool_choice`` with ``"required"`` semantics
          (translate it to the provider shape and do NOT let the model
          answer in free text), a ``max_tokens`` ceiling and a
          ``metadata`` dict (``session_id``, ``node``, ...) to copy into
          log extras — never into the provider request body.
        * **Returned tool-call shape** — exactly one
          :class:`ToolCallResult` whose ``tool_name`` matches a
          requested spec name, whose ``arguments`` are the raw
          (unvalidated) argument dict, with ``provider`` set to
          ``self.name``, ``latency_ms`` measured around the network
          call, optional ``raw_content`` (accompanying free text, for
          audit/debug only) and the provider ``finish_reason``.
        * **Error behavior** — a response without a usable tool call
          raises :class:`LLMResponseError` (WARNING logged; the
          reflexion loop retries); transport/auth/SDK/JSON-decode
          failures raise :class:`LLMAdapterError` (ERROR logged with
          ``exc_info=True`` before raising); SDK exceptions must never
          escape unwrapped.

        Args:
            request: The prepared request.

        Returns:
            The extracted tool call (arguments unvalidated).

        Raises:
            LLMResponseError: When the model produced no usable tool
                call (logged WARNING — model degradation feeding the
                reflexion loop).
            LLMAdapterError: On transport/auth/SDK failures (logged
                ERROR with traceback context before raising).
        """

    async def generate_tool_call_async(self, request: ToolCallRequest) -> ToolCallResult:
        """Async twin of :meth:`generate_tool_call` (G5, phase 1).

        The default implementation offloads :meth:`generate_tool_call` to
        a worker thread via :func:`asyncio.to_thread`, so an awaiting
        event loop is never blocked by the provider round-trip — the
        property async embeddings (FastAPI, LangGraph async graphs)
        actually need.  Every adapter inherits this for free; adapters
        with native async SDK clients may override for lower overhead
        (phase 2, see ``docs/LLM_PROVIDERS.md``).

        Contract: identical semantics, error normalization and logging
        as the synchronous method — same :class:`ToolCallResult`, same
        exception family, thread-safe by construction (the sync
        contract already requires it).

        Args:
            request: The prepared request.

        Returns:
            The extracted tool call (arguments unvalidated).

        Raises:
            LLMResponseError: When the model produced no usable tool
                call (same as the sync path).
            LLMAdapterError: On transport/auth/SDK failures (same as
                the sync path).
        """
        log.debug(
            "async tool call dispatched (thread-offloaded)",
            extra={"provider": self.name,
                   "session_id": request.metadata.get("session_id", "-")},
        )
        return await asyncio.to_thread(self.generate_tool_call, request)

    def healthcheck(self) -> bool:
        """Cheap reachability probe; adapters may override.

        The base implementation performs no I/O — the mock (and any
        adapter without a liveness endpoint) is always considered
        healthy.  Overrides should stay exception-free: report the
        outcome via the return value, never raise.

        Returns:
            ``True`` by default (the Mock is always healthy); ``False``
            when an override detects the endpoint is unreachable.
        """
        healthy = True
        log.debug("healthcheck probe", extra={"provider": self.name, "healthy": healthy})
        return healthy

    def _timed(self, request: ToolCallRequest) -> float:
        """Return elapsed seconds since ``request`` dispatch started.

        Helper for subclasses; starts a fresh timer per call.

        Args:
            request: Unused (kept for signature symmetry in hooks).

        Returns:
            0.0 — subclasses replace with ``time.perf_counter()`` deltas.
        """
        del request
        log.debug("base _timed hook invoked (no-op)", extra={"provider": self.name})
        return 0.0

    @staticmethod
    def _now() -> float:
        """Return a monotonic timestamp for latency measurement.

        Intentionally unlogged: a pure clock read on the
        latency-measurement hot path (called twice per request) with
        nothing meaningful to observe.
        """
        return time.perf_counter()
