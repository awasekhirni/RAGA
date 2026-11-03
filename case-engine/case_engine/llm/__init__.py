"""LLM subpackage: provider-agnostic adapters + the client factory.

Every adapter implements the identical one-method protocol
(:meth:`~case_engine.llm.base.BaseLLMClient.generate_tool_call`): one
schema-gated tool call in, one normalized
:class:`~case_engine.llm.base.ToolCallResult` out.  The engine core
never imports a provider SDK — each adapter lazily imports its SDK at
construction time, so ``pip install case-engine`` suffices for the mock
and extras (``[openai]``, ``[anthropic]``) unlock the hosted providers.

Invariants
----------
* All adapters implement the same protocol; tool-call payloads are
  provider-shaped on the wire but semantically identical once extracted.
* Arguments are returned unvalidated — the enforcement gate
  (``case_engine.enforcement.gate``) is the single source of truth.
* Adapter failures are normalized into the
  :class:`~case_engine.exceptions.LLMAdapterError` family so the
  reflexion loop can retry them; SDK exceptions never leak.

Contents
--------
- :class:`BaseLLMClient`, :class:`ToolCallRequest`, :class:`ToolCallResult`
  — the protocol (``llm.base``).
- :class:`MockLLMClient` — deterministic scripted adapter for tests and
  offline demos (``llm.mock_client``).
- :class:`OpenAIClient`, :class:`OpenAICompatibleClient` — Chat-Completions
  adapters (``llm.openai_client``).
- :class:`AnthropicClient` — Messages-API adapter (``llm.anthropic_client``).
- :class:`OllamaClient`, :class:`VLLMClient` — local-model adapters
  (``llm.ollama_client``).
- :func:`create_llm_client` — provider-key factory used by
  :class:`~case_engine.config.EngineConfig`.

Cross-references: ``docs/LLM_PROVIDERS.md`` (protocol, adapter table,
custom-provider guide) and ``docs/AUDIT_LEDGER.md`` (the trail every
LLM interaction leaves behind).
"""

from __future__ import annotations

from case_engine.exceptions import ConfigurationError
from case_engine.llm.anthropic_client import AnthropicClient
from case_engine.llm.base import BaseLLMClient, ToolCallRequest, ToolCallResult
from case_engine.llm.mock_client import MockLLMClient
from case_engine.llm.ollama_client import OllamaClient, VLLMClient
from case_engine.llm.openai_client import OpenAIClient, OpenAICompatibleClient
from case_engine.logging_setup import get_logger

__all__ = [
    "BaseLLMClient", "ToolCallRequest", "ToolCallResult",
    "MockLLMClient", "OpenAIClient", "OpenAICompatibleClient",
    "AnthropicClient", "OllamaClient", "VLLMClient",
    "create_llm_client",
]

log = get_logger(__name__)


def create_llm_client(provider: str, **kwargs: object) -> BaseLLMClient:
    """Instantiate a provider adapter by key.

    This is the single construction point used by configuration-driven
    setups (CLI, ``EngineConfig``); programmatic users may also
    instantiate adapter classes directly or inject fully custom objects
    satisfying the protocol.

    Args:
        provider: ``"mock"`` | ``"openai"`` | ``"anthropic"`` |
            ``"ollama"`` | ``"vllm"``.
        **kwargs: Forwarded to the adapter constructor (``model``,
            ``base_url``, ``api_key``, ...).  Only the kwarg *names*
            are logged — values may contain credentials.

    Returns:
        A ready adapter instance (constructed lazily; no network I/O
        happens until :meth:`~case_engine.llm.base.BaseLLMClient.generate_tool_call`).

    Raises:
        ConfigurationError: On an unknown provider key — logged at
            ERROR with the available keys before raising.

    Example:
        >>> client = create_llm_client("mock")
        >>> client.name
        'mock'
    """
    factories: dict[str, type[BaseLLMClient]] = {
        "mock": MockLLMClient,
        "openai": OpenAIClient,
        "anthropic": AnthropicClient,
        "ollama": OllamaClient,
        "vllm": VLLMClient,
    }
    if provider not in factories:
        log.error(
            "unknown LLM provider requested",
            extra={"provider": provider, "available": sorted(factories)},
        )
        raise ConfigurationError(
            f"Unknown provider {provider!r}", available=sorted(factories)
        )
    # Kwarg NAMES only — values (api_key, ...) must never hit the logs.
    log.debug("instantiating LLM adapter", extra={"provider": provider, "kwargs": sorted(kwargs)})
    return factories[provider](**kwargs)
