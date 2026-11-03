"""Report models shared by validation, enforcement and the pipeline.

Purpose & role
--------------
These are the *engine's* output contracts (as opposed to the mental-model
input contracts in :mod:`case_engine.schemas.mental_models`): they record
what the enforcement gate decided, what each validation rule concluded,
and what the pipeline retained per generated trace.

Contents
--------
* :class:`GateDecision` — per-tool-call verdict of the enforcement gate
  (Strategy 1 re-validation).
* :class:`RuleReport` — per-rule outcome from node_5.
* :class:`TraceValidationReport` — aggregated verdict for one trace.
* :class:`TraceRecord` — a (possibly regenerated) trace kept in
  ``CASEState.reasoning_traces``.

Invariants
----------
* ``rule_reports`` is empty whenever ``schema_valid`` is False — rules
  never run on a schema-invalid trace (enforced in
  ``case_engine/validation/rules.py``).
* ``TraceRecord.report`` is ``None`` until node_5 has produced a verdict.

Cross-references
----------------
* Producers: ``case_engine.enforcement.gate`` (GateDecision),
  ``case_engine.validation`` (RuleReport / TraceValidationReport),
  ``case_engine.pipeline.nodes`` (TraceRecord).
* Consumers: ``CASEState`` / ``CASEResult``
  (:mod:`case_engine.schemas.pipeline`), synthesis (node_7).
* Docs: ``docs/PIPELINE.md``, ``docs/VALIDATION_RULES.md``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from case_engine.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["GateDecision", "RuleReport", "TraceValidationReport", "TraceRecord"]


class GateDecision(BaseModel):
    """Outcome of the enforcement gate for one tool call (Strategy 1).

    Produced by :class:`case_engine.enforcement.SchemaGate` on every LLM
    tool call: the payload is re-parsed against the typed contract whose
    name matches ``tool_name`` before any side effect is allowed.
    Rejections here are the engine's "fill-in-the-blank" forcing
    function — no response can be synthesized from unvalidated output.

    Attributes:
        approved: ``True`` only when the payload parsed against the
            mental-model contract.
        tool_name: Tool (schema class) name the payload claimed.
        errors: Human-readable validation errors (empty when approved).
        parsed: The validated Pydantic instance, serialized to a dict —
            present only when approved.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(
        ..., description="True only when the payload parsed against the contract"
    )
    tool_name: str = Field(..., description="Tool (schema class) name the payload claimed")
    errors: list[str] = Field(
        default_factory=list,
        description="Human-readable validation errors; empty when approved",
    )
    parsed: dict[str, Any] | None = Field(
        default=None,
        description="Validated instance serialized to a dict; None when rejected",
    )


class RuleReport(BaseModel):
    """Result of executing a single validation rule against a trace.

    One instance per VR* rule run at node_5; the executable heuristics
    live in ``case_engine/validation/rules.py`` (keyed by ``rule_id``).
    Bundled into :class:`TraceValidationReport.rule_reports`.

    Attributes:
        rule_id: e.g. ``"VR005"``.
        rule_name: snake_case rule name.
        rule_type: semantic | structural | mathematical | conditional | lookup.
        passed: Whether the trace satisfied the rule.
        message: Explanation of the verdict, quoting evidence.
        judge_used: Whether an LLM-as-judge verdict was combined with the
            deterministic heuristic.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(..., description='Canonical rule id, e.g. "VR005"')
    rule_name: str = Field(..., description="snake_case rule name")
    rule_type: str = Field(
        ...,
        description="semantic | structural | mathematical | conditional | lookup",
    )
    passed: bool = Field(..., description="Whether the trace satisfied the rule")
    message: str = Field(..., description="Verdict explanation quoting the evidence")
    judge_used: bool = Field(
        default=False,
        description="Whether an LLM-as-judge verdict was combined with the heuristic",
    )


class TraceValidationReport(BaseModel):
    """Full validation verdict for one generated trace.

    Produced by node_5 (``case_engine.validation``) for every trace and
    embedded into :class:`TraceRecord.report` / surfaced in
    ``CASEResult.errors`` via ``failed_rules`` feedback for reflexion.

    Attributes:
        model_id: Mental model the trace belongs to.
        tool_name: Tool/contract name.
        schema_valid: Whether Pydantic parsing succeeded.
        schema_errors: Field-level Pydantic errors (empty when valid).
        rule_reports: Per-rule results (empty when schema invalid — rules
            never run on a schema-invalid trace).
        passed: Overall verdict: schema valid AND all rules pass.
        latency_ms: Wall-clock validation time (compared against the
            ``schema_validation_timeout_ms`` guardrail).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(..., description='Owning mental-model id, e.g. "MM005"')
    tool_name: str = Field(..., description="Tool/contract name the trace targeted")
    schema_valid: bool = Field(..., description="Whether Pydantic parsing succeeded")
    schema_errors: list[str] = Field(
        default_factory=list,
        description="Field-level Pydantic errors; empty when schema_valid",
    )
    rule_reports: list[RuleReport] = Field(
        default_factory=list,
        description="Per-rule results; empty when the schema itself was invalid",
    )
    passed: bool = Field(
        ..., description="Overall verdict: schema valid AND every rule passed"
    )
    latency_ms: float = Field(
        default=0.0,
        description="Wall-clock validation time in milliseconds",
    )

    @property
    def failed_rules(self) -> list[str]:
        """Return the rule ids that failed (empty list when none).

        Consumed by node_6 (reflexion) to focus the regeneration prompt on
        the rejected invariants, and mirrored into
        ``CASEResult.errors`` when retries are exhausted.

        Returns:
            Ordered list of failing ``rule_id`` values.

        Example:
            >>> TraceValidationReport(
            ...     model_id="MM005", tool_name="RootCauseAnalysisTrace",
            ...     schema_valid=True, passed=False,
            ...     rule_reports=[RuleReport(
            ...         rule_id="VR005", rule_name="ensure_root_cause_is_not_human_error",
            ...         rule_type="semantic", passed=False, message="blames human")],
            ... ).failed_rules
            ['VR005']
        """
        failed = [r.rule_id for r in self.rule_reports if not r.passed]
        log.debug(
            "Failed-rule summary computed",
            extra={"model_id": self.model_id, "failed_rules": failed},
        )
        return failed


class TraceRecord(BaseModel):
    """A generated (possibly re-generated) trace retained in pipeline state.

    Stored in ``CASEState.reasoning_traces`` keyed by ``model_id``; the
    raw ``payload`` is kept unvalidated for audit forensics while
    ``report`` carries the node_5 verdict for the latest attempt.

    Attributes:
        model_id: Owning mental model.
        model_name: Human-readable model name.
        tool_name: Contract/tool name.
        payload: The tool-call arguments as received (raw, unvalidated).
        attempts: Number of LLM generation attempts for this trace
            (1 = first shot; >1 means reflexion regenerated it).
        report: Final validation report (``None`` until node_5 ran).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(..., description='Owning mental-model id, e.g. "MM005"')
    model_name: str = Field(
        ..., description="Human-readable model name from the registry"
    )
    tool_name: str = Field(..., description="Contract/tool name used for generation")
    payload: dict[str, Any] = Field(
        ..., description="Raw tool-call arguments as received (unvalidated)"
    )
    attempts: int = Field(
        default=1,
        description="Generation attempts consumed (1 = first shot; >1 = reflexion)",
    )
    report: TraceValidationReport | None = Field(
        default=None, description="Final validation report; None until node_5 ran"
    )
