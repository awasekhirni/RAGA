"""The enforcement gate: every tool call is re-validated server-side.

Security role
-------------
This module implements Strategy 1 of the C.A.S.E. spec ("Schema-Gated
Tool Invocation / the Pydantic Gate") with an explicit **zero-trust**
posture: even when the provider already constrained generation to a
JSON Schema, the engine re-parses and re-validates the raw payload
against the authoritative typed contract (``extra="forbid"``) before
any downstream node may consume it.  Provider-side schema enforcement
is treated as an optimisation, never as a trust anchor: payloads can be
truncated, replayed or hand-crafted between provider and engine, so
this gate is the single point where "the LLM said so" becomes "the
engine proved so".

Contents
--------
- :class:`SchemaGate` -- resolves *tool name* -> contract class and
  returns a :class:`~case_engine.schemas.reports.GateDecision` per
  tool call.

Invariants
----------
- The gate NEVER trusts prior validation (its own or the provider's):
  ``SchemaGate.check`` always re-runs ``model_validate`` on the exact
  raw argument dict it received.
- Hallucinated fields fail the call: contracts are declared with
  ``extra="forbid"``, so an over-eager payload is rejected wholesale
  and the "fill-in-the-blank" regeneration loop is forced.
- Unknown tool names are **enforcement faults** (raise
  :class:`~case_engine.exceptions.ToolCallRejectedError`), distinct
  from normal schema rejections (which return ``approved=False`` and
  are retryable via reflexion); see docs/ARCHITECTURE.md §1.3.
- Fail-closed: there is no code path that returns ``approved=True``
  without a successful Pydantic parse of the exact payload received,
  and validation infrastructure errors are never converted into an
  approval.

Cross-references
----------------
- docs/ARCHITECTURE.md -- strategy mapping and deviation rationale
  (§1.1, §1.2, §1.3).
- docs/VALIDATION_RULES.md -- the semantic rule battery (VR001-VR016)
  applied *after* this gate by the ValidationEngine.
"""

from __future__ import annotations

from pydantic import ValidationError

from case_engine.exceptions import ToolCallRejectedError
from case_engine.logging_setup import get_logger
from case_engine.registry.models import CognitiveRegistry
from case_engine.schemas.mental_models import SCHEMA_BY_MODEL_ID
from case_engine.schemas.reports import GateDecision

__all__ = ["SchemaGate"]

log = get_logger(__name__)


class SchemaGate:
    """Validate LLM tool-call payloads against mental-model contracts.

    The gate is the runtime half of Strategy 1: given the tool name the
    LLM *claimed* and the raw argument dict it produced, it resolves the
    authoritative Pydantic contract from the registry, re-validates the
    payload zero-trust and returns a structured
    :class:`~case_engine.schemas.reports.GateDecision`.  Rejections are
    *data* (``approved=False`` plus field-level errors) so the reflexion
    loop can regenerate a compliant payload; only unknown tool names
    raise, because a claimed-but-nonexistent tool is an enforcement
    fault, not a fixable payload problem.

    Security role: this is the last line of defence before any generated
    content enters pipeline state.  It runs on every attempt, including
    attempts the provider already schema-constrained.

    Attributes:
        _registry: Consistency-checked
            :class:`~case_engine.registry.models.CognitiveRegistry`
            used to resolve tool names to contract classes.  The gate
            holds no other state and is safe to share across sessions.

    Example:
        >>> from case_engine.registry import CognitiveRegistry
        >>> gate = SchemaGate(CognitiveRegistry.load_default())
        >>> decision = gate.check(
        ...     "RootCauseAnalysisTrace",
        ...     {"problem_statement": "stale data", "why_1": "w1", "why_2": "w2",
        ...      "why_3": "w3", "why_4": "w4", "why_5": "w5",
        ...      "validated_root_cause": "no alerting on queue depth",
        ...      "systemic_countermeasure": "add alert", "confidence": 0.9},
        ... )
        >>> decision.approved
        True
    """

    def __init__(self, registry: CognitiveRegistry) -> None:
        """Bind the gate to the cognitive registry.

        The gate holds no mutable state; one instance may serve every
        session and thread.  The registry is consistency-checked at its
        own construction time, so broken registry data fails fast there
        rather than mid-session here.

        Args:
            registry: Consistency-checked registry used to resolve tool
                names to contracts.

        Side effects:
            Emits a DEBUG ``SchemaGate initialised`` log record; no I/O.
        """
        self._registry = registry
        log.debug("SchemaGate initialised")

    def check(self, tool_name: str, arguments: dict) -> GateDecision:
        """Validate *arguments* against the contract registered as *tool_name*.

        Sequence: registry lookup (tool name -> model definition),
        contract lookup (algorithm id -> Pydantic class), then strict
        ``model_validate`` with ``extra="forbid"``.  Field-level Pydantic
        errors are flattened into human-readable strings (all are
        returned; at most 5 are logged) so the reflexion loop can
        regenerate a corrected payload.

        Zero-trust reminder: this method intentionally accepts no
        pre-validated models and consults no cached decisions -- the
        payload received here is the payload that is checked.

        Args:
            tool_name: The tool (schema class) name claimed by the LLM.
            arguments: Raw tool-call argument dict (untrusted input).

        Returns:
            A :class:`GateDecision`; ``approved=True`` carries the parsed
            model serialized to a plain dict, ``approved=False`` carries
            the validation errors (payload content is never logged above
            DEBUG; only error *strings* and the tool name appear in logs).

        Raises:
            ToolCallRejectedError: Only when *tool_name* is unknown
                (unknown-tool is treated as an enforcement fault rather
                than a normal rejection).  Logged at ERROR before the
                raise; the underlying registry exception is chained as
                ``__cause__``.

        Logging:
            DEBUG on entry and approval, WARNING on schema rejection,
            ERROR before the enforcement-fault raise.
        """
        log.debug(
            "Gate check started",
            extra={
                "tool": tool_name,
                "argument_keys": (
                    sorted(str(k) for k in arguments)[:12]
                    if isinstance(arguments, dict)
                    else []
                ),
            },
        )
        try:
            model = self._registry.get_by_tool_name(tool_name)
        except Exception as exc:  # RegistryError -> enforcement fault
            log.error(
                "Unknown tool name - raising enforcement fault",
                extra={"tool": tool_name, "cause": str(exc)},
            )
            raise ToolCallRejectedError(
                f"Unknown tool name '{tool_name}' - cannot resolve contract",
                tool_name=tool_name,
            ) from exc

        contract = SCHEMA_BY_MODEL_ID[model.algorithm_id]
        try:
            parsed = contract.model_validate(arguments)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_url=False)
            ]
            log.warning(
                "Gate REJECTED tool call",
                extra={"tool": tool_name, "errors": errors[:5]},
            )
            return GateDecision(approved=False, tool_name=tool_name, errors=errors)

        log.debug("Gate approved tool call", extra={"tool": tool_name})
        return GateDecision(approved=True, tool_name=tool_name, parsed=parsed.model_dump())
