"""Local-model adapters: Ollama and vLLM (both OpenAI-compatible).

Both daemons expose an OpenAI-compatible Chat-Completions endpoint, so
each adapter is a thin re-targeting of
:class:`~case_engine.llm.openai_client.OpenAICompatibleClient`: the
forced-tool-call semantics, ``tool_calls[0]`` extraction, error
normalization and logging behaviour are inherited unchanged.

Invariants
----------
* Same protocol as every other adapter — one
  :class:`~case_engine.llm.base.ToolCallRequest` in, one
  :class:`~case_engine.llm.base.ToolCallResult` out, arguments
  unvalidated (the enforcement gate owns validation).
* The ``openai`` SDK remains a lazy import (inherited from the parent);
  the core install never needs it.
* Dummy bearer keys (``"ollama"`` / ``"vllm"`` / user-supplied) are
  configuration, never logged.

Contents
--------
- :class:`OllamaClient` — local Ollama daemon (default
  ``localhost:11434/v1``).
- :class:`VLLMClient` — a vLLM OpenAI server (e.g. ``:8000/v1``).

Cross-references: ``docs/LLM_PROVIDERS.md`` §2 (adapter table) and §4
(constrained-decoding notes for self-hosted models).
"""

from __future__ import annotations

from case_engine.llm.openai_client import OpenAICompatibleClient
from case_engine.logging_setup import get_logger

__all__ = ["OllamaClient", "VLLMClient"]

log = get_logger(__name__)


class OllamaClient(OpenAICompatibleClient):
    """Adapter for Ollama's OpenAI-compatible endpoint.

    Point it at a locally running Ollama daemon (``ollama serve``); the
    model must have been pulled beforehand (``ollama pull <tag>``).
    Because Ollama's tool-support varies per model family, treat
    provider-side forcing as best-effort — the enforcement gate still
    re-validates every payload.

    Raises:
        LLMProviderUnavailableError: Inherited from
            :class:`~case_engine.llm.openai_client.OpenAIClient` when
            the ``openai`` package is missing and no client was
            injected.

    Example:
        >>> client = OllamaClient(model="llama3.1:8b")
        >>> client.name
        'ollama'
    """

    name = "ollama"

    def __init__(
        self,
        model: str = "llama3.1:8b",
        base_url: str = "http://localhost:11434/v1",
        temperature: float = 0.0,
        timeout_s: float = 120.0,
        client: object | None = None,
    ) -> None:
        """Point the OpenAI-compatible adapter at the local Ollama daemon.

        Args:
            model: Ollama model tag.
            base_url: Ollama OpenAI endpoint (default port 11434).
            temperature: Sampling temperature.
            timeout_s: Request timeout (local inference can be slow).
            client: Pre-built SDK client (test injection).

        Raises:
            LLMProviderUnavailableError: When the ``openai`` SDK is
                required but not installed (logged ERROR with the
                chained ImportError by the parent constructor).
        """
        super().__init__(
            base_url=base_url, model=model, api_key="ollama",
            temperature=temperature, timeout_s=timeout_s, client=client,
        )
        self.name = "ollama"
        log.debug(
            "ollama adapter configured",
            extra={"base_url": base_url, "model": model},
        )


class VLLMClient(OpenAICompatibleClient):
    """Adapter for a vLLM OpenAI server.

    Use this for self-hosted serving of any model with a Chat-Completions
    surface (``vllm serve <model>``).  The dummy bearer exists only for
    servers fronted by an authenticating proxy.

    Raises:
        LLMProviderUnavailableError: Inherited from
            :class:`~case_engine.llm.openai_client.OpenAIClient` when
            the ``openai`` package is missing and no client was
            injected.

    Example:
        >>> client = VLLMClient(base_url="http://10.0.0.5:8000/v1", model="Qwen2.5-7B-Instruct")
    """

    name = "vllm"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "vllm",
        temperature: float = 0.0,
        timeout_s: float = 120.0,
        client: object | None = None,
    ) -> None:
        """Point the OpenAI-compatible adapter at a vLLM server.

        Args:
            base_url: vLLM endpoint, e.g. ``http://host:8000/v1``.
            model: Served model id.
            api_key: Dummy bearer for servers that require one.
                Never logged.
            temperature: Sampling temperature.
            timeout_s: Request timeout.
            client: Pre-built SDK client (test injection).

        Raises:
            LLMProviderUnavailableError: When the ``openai`` SDK is
                required but not installed (logged ERROR with the
                chained ImportError by the parent constructor).
        """
        super().__init__(
            base_url=base_url, model=model, api_key=api_key,
            temperature=temperature, timeout_s=timeout_s, client=client,
        )
        self.name = "vllm"
        log.debug(
            "vllm adapter configured",
            extra={"base_url": base_url, "model": model},
        )
