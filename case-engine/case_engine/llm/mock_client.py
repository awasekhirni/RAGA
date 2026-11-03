"""Deterministic MockLLMClient — offline demos and reproducible tests.

The mock serves payloads from a *script* keyed by tool name.  A failure
plan lets tests inject deterministic misbehaviour (missing tool call,
invalid arguments, wrong tool) to exercise the reflexion loop and the
escalation path without any network or randomness.

Invariants
----------
* Fully deterministic: identical script + failure plan ⇒ identical
  result sequence, so the whole test suite and CLI demos run offline.
* Implements the exact same protocol as the real adapters
  (:class:`~case_engine.llm.base.BaseLLMClient.generate_tool_call`),
  including error normalization (``LLMResponseError`` for scripted
  refusals) — tests exercise the same code paths as production.
* Served payloads are **deep-copied** per call, so callers can mutate
  a result without corrupting the script.

Contents
--------
- :class:`MockLLMClient` — scripted client with ``fail_next`` failure
  injection and registry auto-scripting.

Cross-references: ``docs/LLM_PROVIDERS.md`` §6 (failure-mode semantics)
and ``docs/AUDIT_LEDGER.md`` (the audit trail these mock sessions
produce).
"""

from __future__ import annotations

import copy
import json
from typing import Any

from case_engine.exceptions import LLMResponseError
from case_engine.llm.base import BaseLLMClient, ToolCallRequest, ToolCallResult
from case_engine.logging_setup import get_logger
from case_engine.registry.models import CognitiveRegistry

__all__ = ["MockLLMClient"]

log = get_logger(__name__)


class MockLLMClient(BaseLLMClient):
    """Scripted, deterministic stand-in for any provider.

    Attributes:
        name: ``"mock"``.

    Example:
        >>> client = MockLLMClient(script={"MyTool": {"a": 1}})
        >>> res = client.generate_tool_call(ToolCallRequest(
        ...     system_prompt="s", user_prompt="u",
        ...     tool_specs=[{"type": "function",
        ...                  "function": {"name": "MyTool", "parameters": {}}}]))
        >>> res.arguments
        {'a': 1}
    """

    name = "mock"

    def __init__(
        self,
        script: dict[str, dict[str, Any]] | None = None,
        default_tool: str | None = None,
        failure_plan: list[str] | None = None,
    ) -> None:
        """Create a mock client.

        Args:
            script: ``tool_name -> payload`` mapping served verbatim.  When
                ``None`` the script is auto-populated with every registry
                model's gold example (zero-configuration offline mode).
            default_tool: Optional fallback tool served when the requested
                tool is not scripted (useful for negative tests).
            failure_plan: FIFO list of failure modes consumed by
                :meth:`generate_tool_call`:
                ``"no_tool_call"`` | ``"invalid_args"`` | ``"unknown_tool"``.

        Raises:
            RegistryError: Propagated from
                ``CognitiveRegistry.load_default()`` when the default
                registry cannot be loaded during auto-scripting (logged
                by the registry module).
        """
        if script is None:
            script = {
                m.tool_name: dict(m.examples[0])
                for m in CognitiveRegistry.load_default().all_models()
                if m.examples
            }
            log.debug("MockLLMClient auto-scripted from default registry", extra={"tools": len(script)})
        self._script: dict[str, dict[str, Any]] = dict(script)
        self._default_tool = default_tool
        self._failure_plan: list[str] = list(failure_plan or [])
        log.debug(
            "MockLLMClient ready",
            extra={"scripted_tools": sorted(self._script), "failures": len(self._failure_plan)},
        )

    @classmethod
    def from_registry_examples(cls, registry: CognitiveRegistry) -> MockLLMClient:
        """Build a client scripted with every registry model's gold example.

        This powers the zero-configuration offline demo: whatever the
        router selects, the mock answers with the spec's own valid
        example payload for that tool.

        Args:
            registry: Loaded cognitive registry.

        Returns:
            A fully scripted :class:`MockLLMClient` (construction itself
            logs a DEBUG "ready" line with the scripted tool names).
        """
        script = {
            m.tool_name: dict(m.examples[0])
            for m in registry.all_models()
            if m.examples
        }
        log.info("MockLLMClient scripted from registry examples", extra={"tools": len(script)})
        return cls(script=script)

    def fail_next(self, times: int = 1, mode: str = "invalid_args") -> MockLLMClient:
        """Queue deterministic failures for the next *times* calls.

        Arming a failure plan is logged at WARNING (as is its later
        consumption in :meth:`generate_tool_call`) so test runs and
        demos make the injected degradation visible in the logs.

        Args:
            times: How many upcoming calls fail.
            mode: ``"no_tool_call"`` | ``"invalid_args"`` | ``"unknown_tool"``.

        Returns:
            ``self`` (fluent).

        Raises:
            ValueError: On an unknown mode (programmer error).  Logged at
                ERROR before raising.
        """
        if mode not in {"no_tool_call", "invalid_args", "unknown_tool"}:
            log.error("unknown mock failure mode", extra={"mode": mode})
            raise ValueError(f"Unknown failure mode: {mode!r}")
        self._failure_plan.extend([mode] * times)
        log.warning(
            "mock failure armed",
            extra={"mode": mode, "times": times, "pending": len(self._failure_plan)},
        )
        return self

    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        """Serve the scripted payload for the requested tool.

        Each call logs DEBUG for the request and the scripted-response
        selection, and WARNING whenever a scripted failure mode is
        consumed or a requested tool has no script.

        Args:
            request: The prepared request (single-tool lists expected).

        Returns:
            A :class:`ToolCallResult` carrying a **deep copy** of the
            scripted payload so callers can mutate freely.

        Raises:
            LLMResponseError: On scripted ``no_tool_call`` failures or when
                the requested tool has no script (simulates a refusal).
                Both are logged at WARNING before raising; both are
                reflexion-routed by the pipeline.
        """
        started = self._now()
        tool_name = request.primary_tool_name
        mode = self._failure_plan.pop(0) if self._failure_plan else None
        log.debug(
            "Mock call", extra={"tool": tool_name, "mode": mode or "valid",
                                "session": request.metadata.get("session_id", "-")}
        )
        if mode is not None:
            log.warning(
                "mock failure consumed",
                extra={"mode": mode, "tool": tool_name,
                       "pending": len(self._failure_plan)},
            )

        if mode == "no_tool_call":
            log.warning(
                "mock scripted refusal: free text instead of tool call",
                extra={"tool": tool_name,
                       "session": request.metadata.get("session_id", "-")},
            )
            raise LLMResponseError(
                "[mock] scripted failure: model produced free text instead of a tool call",
                provider=self.name, tool=tool_name,
            )

        if mode == "unknown_tool":
            # Simulates a model hallucinating a tool name; the enforcement
            # gate must reject this (unknown tool -> ToolCallRejectedError).
            log.warning(
                "mock scripted hallucinated tool name (gate must reject)",
                extra={"requested": tool_name, "served": "DefinitelyNotARealTool"},
            )
            return ToolCallResult(
                tool_name="DefinitelyNotARealTool",
                arguments={"__mock_unknown_tool__": True},
                provider=self.name,
                latency_ms=0.0,
                finish_reason="tool_calls",
            )

        served_tool = tool_name
        if served_tool not in self._script:
            if self._default_tool and self._default_tool in self._script:
                log.debug(
                    "falling back to default mock tool",
                    extra={"requested": served_tool, "served": self._default_tool},
                )
                served_tool = self._default_tool
            else:
                log.warning(
                    "no scripted payload for requested tool (mock refusal)",
                    extra={"tool": served_tool,
                           "scripted": sorted(self._script)[:10]},
                )
                raise LLMResponseError(
                    f"[mock] no scripted payload for tool {served_tool!r}",
                    provider=self.name, tool=served_tool,
                )

        payload = copy.deepcopy(self._script[served_tool])
        if mode == "invalid_args":
            payload = {"__mock_invalid__": "this payload violates every contract"}
        log.debug(
            "scripted response selected",
            extra={"requested": tool_name, "served": served_tool,
                   "mode": mode or "valid"},
        )

        latency = (self._now() - started) * 1000.0
        return ToolCallResult(
            tool_name=served_tool,
            arguments=payload,
            provider=self.name,
            latency_ms=round(latency, 3),
            raw_content="",
            finish_reason="tool_calls",
        )

    def describe_script(self) -> str:
        """Return a pretty JSON dump of the script (debugging helper).

        Intended for test diagnostics only — never point it at a script
        containing production-like payloads and paste the output into
        shared logs.

        Returns:
            The full script as indented JSON (tool-name keyed payloads).

        Example:
            >>> MockLLMClient(script={"T": {"a": 1}}).describe_script()
            '{\\n  "T": {\\n    "a": 1\\n  }\\n}'
        """
        log.debug("mock script dumped", extra={"tools": len(self._script)})
        return json.dumps(self._script, indent=2, ensure_ascii=False)
