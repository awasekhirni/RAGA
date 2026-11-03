"""Pipeline state and result contracts.

Purpose & role
--------------
``CASEState`` mirrors the spec's ``execution_pipeline.state_schema`` and
adds the operational fields the deterministic nodes need (denoted
*engine extension* below).  It is a ``TypedDict`` over plain JSON-safe
values so the exact same state dict can be driven by the pure-Python
state machine **or** the optional LangGraph adapter.
``CASEResult`` is the immutable, user-facing outcome returned by one
``CASEEngine.run`` invocation.

Contents
--------
* :class:`CynefinDomain` / :class:`SpecialistDomain` — routing domain keys.
* :class:`PipelinePhase` / :class:`AuditEventType` — audit-trail labels.
* :class:`CASEState` — mutable node-to-node pipeline state (TypedDict).
* :class:`CASEResult` — final session outcome (Pydantic model).

Invariants
----------
* State values must stay JSON-safe (no datetimes/objects) so the state
dict round-trips through ledgers, logs and the LangGraph adapter.
* ``AuditEventType`` names are a stable contract for downstream
  SIEM/analytics consumers of the hash-chained ledger (see
  ``docs/AUDIT_LEDGER.md``) — never rename a member.
* Domain keys match the registry routing matrix exactly
  (``case_engine/registry/rule_data.py``).

Cross-references
----------------
* Executed by: ``case_engine.pipeline.state_machine`` / ``nodes``
  (see ``docs/PIPELINE.md`` for the node graph node_1 … node_8).
* Trace/report models: :mod:`case_engine.schemas.reports`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from case_engine.logging_setup import get_logger
from case_engine.schemas.reports import TraceRecord

log = get_logger(__name__)

__all__ = [
    "CynefinDomain",
    "SpecialistDomain",
    "PipelinePhase",
    "AuditEventType",
    "TokenUsage",
    "CASEState",
    "CASEResult",
]


class CynefinDomain(str, Enum):
    """The four canonical Cynefin domains (spec node_1 output).

    The ``value`` strings are the canonical domain keys used verbatim in
    the registry routing matrix, the router and audit events — never
    rename them.  Produced by node_1 (MM013 classification), consumed by
    node_2 (routing).

    Example:
        >>> CynefinDomain.COMPLEX.value
        'Complex'
    """

    CLEAR = "Clear"
    COMPLICATED = "Complicated"
    COMPLEX = "Complex"
    CHAOTIC = "Chaotic"


class SpecialistDomain(str, Enum):
    """Specialist routing domains supported by the spec routing matrix.

    Keys appear verbatim in ``ROUTING_MATRIX``
    (``case_engine/registry/rule_data.py``) and are selectable as
    operator presets; they bypass Cynefin classification when provided.

    Example:
        >>> SpecialistDomain.LEGACY_REFACTORING.value
        'Legacy_Refactoring'
    """

    LEGACY_REFACTORING = "Legacy_Refactoring"
    AI_ALIGNMENT_METRICS = "AI_Alignment_Metrics"
    IOT_TELEMETRY_DIAGNOSIS = "IoT_Telemetry_Diagnosis"
    TEAM_ORG_DESIGN = "Team_Org_Design"


class PipelinePhase(str, Enum):
    """Human-readable phase labels attached to the audit trail.

    Each label maps to one pipeline node (see ``docs/PIPELINE.md``):
    intent_classification (node_1), model_routing (node_2),
    schema_injection (node_3), generation (node_4), validation (node_5),
    reflexion (node_6), synthesis (node_7), escalation (node_8) and the
    terminal ``complete`` marker.

    Example:
        >>> PipelinePhase.VALIDATION.value
        'validation'
    """

    INTENT_CLASSIFICATION = "intent_classification"
    MODEL_ROUTING = "model_routing"
    SCHEMA_INJECTION = "schema_injection"
    GENERATION = "generation"
    VALIDATION = "validation"
    REFLEXION = "reflexion"
    SYNTHESIS = "synthesis"
    ESCALATION = "escalation"
    COMPLETE = "complete"


class AuditEventType(str, Enum):
    """Event types written to the hash-chained audit ledger.

    These names are stable contract: downstream SIEM/analytics must be
    able to rely on them.  Events are emitted by the pipeline nodes and
    appended by the ledger backends (``case_engine/audit/``); the chain
    integrity semantics are documented in ``docs/AUDIT_LEDGER.md``.

    Example:
        >>> AuditEventType.TRACE_VALIDATED.value
        'TRACE_VALIDATED'
    """

    SESSION_START = "SESSION_START"
    DOMAIN_PRESET = "DOMAIN_PRESET"
    DOMAIN_CLASSIFIED = "DOMAIN_CLASSIFIED"
    MODELS_SELECTED = "MODELS_SELECTED"
    SCHEMAS_INJECTED = "SCHEMAS_INJECTED"
    TRACE_GENERATED = "TRACE_GENERATED"
    TRACE_REJECTED = "TRACE_REJECTED"
    TRACE_VALIDATED = "TRACE_VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REFLEXION_RETRY = "REFLEXION_RETRY"
    TRACE_LEDGER_WRITE = "TRACE_LEDGER_WRITE"
    HOOK_FIRED = "HOOK_FIRED"
    ESCALATED = "ESCALATED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    SESSION_FAILED = "SESSION_FAILED"
    TEE_DECISION = "TEE_DECISION"
    FALLBACK_APPLIED = "FALLBACK_APPLIED"
    # G7 (v2.2.0): per-session LLM token accounting (counts only — never
    # prompt/completion content).  Emitted once per session by the terminal
    # node (node_7 synthesis / node_8 escalation).
    LLM_USAGE = "LLM_USAGE"


class TokenUsage(BaseModel):
    """Aggregate LLM token accounting for one session (G7).

    Counts only — prompt and completion *content* never appears here or
    anywhere in the audit payload; the ``LLM_USAGE`` ledger event carries
    exactly these fields so operators can answer "what did this session
    cost?" from the ledger alone.

    Attributes:
        calls: Number of LLM requests issued (all nodes, all models).
        prompt_tokens: Sum of provider-reported prompt (input) tokens;
            ``0`` when the provider did not report usage.
        completion_tokens: Sum of provider-reported completion (output)
            tokens; ``0`` when unreported.
        total_tokens: ``prompt_tokens + completion_tokens`` (the engine
            does not trust provider ``total`` fields, which are not
            uniform across providers).

    Example:
        >>> u = TokenUsage()
        >>> u.add(prompt_tokens=120, completion_tokens=48)
        >>> u.add(prompt_tokens=30, completion_tokens=10)
        >>> u.total_tokens
        208
    """

    model_config = ConfigDict(extra="forbid")

    calls: int = Field(default=0, ge=0, description="Number of LLM requests issued")
    prompt_tokens: int = Field(
        default=0, ge=0, description="Sum of provider-reported prompt tokens"
    )
    completion_tokens: int = Field(
        default=0, ge=0, description="Sum of provider-reported completion tokens"
    )
    total_tokens: int = Field(
        default=0, ge=0, description="prompt_tokens + completion_tokens"
    )

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Accumulate one provider response's usage into this aggregate.

        Mutating convenience for the pipeline's usage accumulator; keeps
        ``total_tokens`` consistent by recomputation rather than by
        trusting caller-supplied totals.

        Args:
            prompt_tokens: Prompt (input) tokens reported by the provider.
            completion_tokens: Completion (output) tokens reported by the
                provider.
        """
        self.calls += 1
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        log.debug(
            "token usage accumulated",
            extra={"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                   "completion_tokens": self.completion_tokens},
        )


class CASEState(TypedDict, total=False):
    """Mutable pipeline state passed node-to-node (JSON-safe values only).

    Spec fields:
        session_id: UUID string for this reasoning session.
        user_query: Raw user problem statement.
        domain_classification: Cynefin domain (set by node_1).
        selected_model_ids: Model ids chosen by node_2.
        reasoning_traces: ``model_id -> TraceRecord`` dict (serialized).
        validation_errors: ``model_id -> feedback text`` for reflexion.
        retry_count: Reflexion cycles consumed so far.
        all_valid: Whether every trace passed node_5.
        final_response: Synthesized user-facing answer (node_7/8).

    Engine-extension fields:
        current_node: Node id currently executing / last completed.
        next_node: Outcome-driven next node (validated by the machine).
        phase: Current :class:`PipelinePhase` label.
        forced_model_ids: Operator override bypassing affinity routing.
        domain_source: ``"classified"`` or ``"preset"``.
        tool_specs: JSON tool specs injected at node_3.
        error_detail: Structured error info for escalation packaging.
        escalated: ``True`` when node_8 ran.
        started_at / finished_at: ISO timestamps for the session.

    PII/TEE extension fields (G1 — written by node_1, read by the
    escalation/synthesis packaging):
        pii_detected: ``True`` when the node_1 detector found PII.
        pii_categories: Distinct detected category strings (no raw values).
        tee_attested: Operator TEE attestation carried into the session.
        tee_decision: Outcome string recorded in the ``TEE_DECISION``
            event (``"escalated"`` | ``"allow_with_flags"`` |
            ``"tee_attested"`` | ``"no_pii"``).

    Fallback-policy extension fields (G2):
        abort_requested: ``True`` when ``fallback_behavior="abort_session"
            exhausted reflexion — node_8 closes the session as aborted.
        passthrough_accepted: ``True`` when
            ``fallback_behavior="passthrough_with_flags"`` let failed
            traces proceed to synthesis (always paired with loud logs
            and a ``FALLBACK_APPLIED`` audit event).

    Token-accounting extension field (G7):
        llm_usage: JSON-safe accumulator ``{"calls": int,
            "prompt_tokens": int, "completion_tokens": int}`` updated by
            node_1/node_4 after every LLM call; terminal nodes persist it
            into the ``LLM_USAGE`` audit event and the result's
            ``token_usage``.

    Example:
        >>> CASEState(session_id="abc", user_query="why churn?")["user_query"]
        'why churn?'
    """

    session_id: str
    user_query: str
    context: dict[str, Any]
    domain_classification: str | None
    domain_source: str | None
    selected_model_ids: list[str]
    forced_model_ids: list[str] | None
    reasoning_traces: dict[str, Any]
    validation_errors: dict[str, str]
    retry_count: int
    all_valid: bool
    final_response: str | None
    current_node: str | None
    next_node: str | None
    phase: str | None
    tool_specs: list[dict[str, Any]]
    error_detail: dict[str, Any] | None
    escalated: bool
    started_at: str
    finished_at: str | None
    pii_detected: bool
    pii_categories: list[str]
    tee_attested: bool
    tee_decision: str | None
    abort_requested: bool
    passthrough_accepted: bool
    llm_usage: dict[str, int]


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Default factory for :attr:`CASEResult.started_at`; tz-aware by design.

    Returns:
        e.g. ``"2026-01-15T09:30:12.123456+00:00"``.

    Example:
        >>> _utcnow().endswith("+00:00")
        True
    """
    now = datetime.now(UTC).isoformat()
    log.debug("Result start timestamp generated", extra={"started_at": now})
    return now


class CASEResult(BaseModel):
    """Final, user-facing outcome of one ``CASEEngine.run`` invocation.

    Produced by the engine after node_7 (synthesis) or node_8
    (escalation) completes; it bundles the selected models, every trace
    with its validation report, and the audit-ledger tail hash so callers
    get a single serializable artifact per session.

    Attributes:
        session_id: The session that produced this result.
        user_query: Original problem statement.
        domain_classification: Cynefin (or preset) domain used for routing.
        domain_source: ``"classified"`` | ``"preset"``.
        selected_model_ids: Mental models that executed.
        traces: One record per generated trace, including validation.
        all_valid: Whether the session concluded with fully valid traces.
        final_response: The synthesized answer (or escalation notice).
        escalated: Whether the session was handed to human review.
        retry_count: Reflexion cycles consumed.
        errors: Remaining validation feedback per model (empty on success).
        started_at / finished_at: Session wall-clock boundaries.
        audit_event_count: Number of ledger entries written.
        last_audit_hash: Tail hash of the ledger chain after this session.

    PII/TEE + fallback-policy extensions (G1/G2, all defaulting to the
    pre-G1 behavior so existing consumers are unaffected):
        pii_flagged: PII was detected in this session (categories in
            ``pii_categories``).
        pii_categories: Distinct detected category strings.
        tee_decision: The recorded TEE decision, when one was made.
        aborted: Session closed by ``abort_session`` policy (escalated
            stays ``False`` — nobody was asked to review it).
        passthrough: Failed traces were allowed through to synthesis
            under ``passthrough_with_flags`` (``all_valid`` is ``False``
            in that case).
        token_usage: Aggregate LLM token accounting for the session (G7)
            — zeroed when the provider reports no usage data.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., description="UUID of the reasoning session")
    user_query: str = Field(..., description="Original raw problem statement")
    domain_classification: str | None = Field(
        default=None,
        description="Cynefin or specialist-preset domain used for routing",
    )
    domain_source: str | None = Field(
        default=None, description='"classified" (node_1) or "preset" (operator override)'
    )
    selected_model_ids: list[str] = Field(
        default_factory=list,
        description="Algorithm ids of the models that executed, in execution order",
    )
    traces: list[TraceRecord] = Field(
        default_factory=list,
        description="One TraceRecord per generated trace, including validation reports",
    )
    all_valid: bool = Field(
        default=False, description="True when every trace passed node_5 validation"
    )
    final_response: str | None = Field(
        default=None,
        description="Synthesized user-facing answer, or the escalation notice",
    )
    escalated: bool = Field(
        default=False, description="True when the session went to human review (node_8)"
    )
    retry_count: int = Field(
        default=0, description="Reflexion regeneration cycles consumed"
    )
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="Remaining validation feedback per model id; empty on success",
    )
    started_at: str = Field(
        default_factory=_utcnow, description="ISO-8601 UTC session start (auto-filled)"
    )
    finished_at: str | None = Field(
        default=None, description="ISO-8601 UTC session end; None until completion"
    )
    audit_event_count: int = Field(
        default=0, description="Number of audit-ledger entries written this session"
    )
    last_audit_hash: str | None = Field(
        default=None, description="Tail hash of the hash-chained ledger after this session"
    )
    pii_flagged: bool = Field(
        default=False,
        description="True when the node_1 PII detector found PII in the session (G1)",
    )
    pii_categories: list[str] = Field(
        default_factory=list,
        description="Distinct PII category strings detected (never raw values)",
    )
    tee_decision: str | None = Field(
        default=None,
        description='TEE decision recorded in the TEE_DECISION audit event '
        '("escalated" | "allow_with_flags" | "tee_attested" | "policy_disabled")',
    )
    aborted: bool = Field(
        default=False,
        description='True when fallback_behavior="abort_session" closed the session',
    )
    passthrough: bool = Field(
        default=False,
        description='True when failed traces were passed through under '
        '"passthrough_with_flags" (all_valid is False in that case)',
    )
    token_usage: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Aggregate LLM token accounting for this session (G7)",
    )
