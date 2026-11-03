"""The 8 pipeline nodes of the C.A.S.E. execution pipeline.

Purpose & role
--------------
This module is the *deterministic control plane* of the engine: the eight
node functions that advance one reasoning session from a raw user query to
either a synthesized answer (node_7) or a human escalation (node_8).  Each
node implements one step of the spec's ``execution_pipeline`` and is wired
together by :mod:`case_engine.pipeline.state_machine`, which validates every
transition against the declared ``EDGES`` table before the next node runs.

Node contract
-------------
Every node is a pure function::

    def node_x(state: CASEState, ctx: EngineContext) -> tuple[str, CASEState]

returning ``(next_node_id, new_state)``.  Invariants:

* Nodes are **pure functions of ``(state, ctx)``** — no hidden I/O except
  through ``ctx``; every node audits its outcome via ``ctx.audit`` before
  returning.
* Nodes never mutate the input state (they copy via :func:`_copy`); nested
  dicts (traces, errors) are replaced wholesale, never mutated in place.
* Nodes never choose actions outside their declared transition set — the
  state machine raises :class:`TopologyViolationError` otherwise.
* The LLM is consulted ONLY for tool-call payloads (content); control flow
  is 100% deterministic code (Strategy 2 — docs/ARCHITECTURE.md §1.1).

Failure / rollback semantics
----------------------------
A failing node records validation feedback and sets ``reflexion_target`` to
*itself* before routing to node_6, so reflexion regenerates the stage that
actually failed instead of blindly retrying node_4.  When node_6 exhausts
the reflexion budget it escalates to node_8, which packages the *partial*
traces and validation errors for a human reviewer (fail-closed, never
silent).

Contents
--------
- ``ENTRY_NODE`` / ``END_NODE``: graph boundary sentinels.
- ``node_1`` … ``node_8``: the eight spec pipeline nodes.
- ``NODES``: node-id -> callable registry consumed by the state machine.
- Private helpers: ``_copy``, ``_truncate``, ``_payload_hash``,
  ``_user_prompt``, ``_trace_record``, ``_summarize_trace``,
  ``_record_usage`` (G7 token accounting), ``_usage_event_payload``.

Deviations from the spec graph (documented in docs/PIPELINE.md):
* node_6 (reflexion) returns to the **node that failed**
  (``state['reflexion_target']``) instead of unconditionally to node_4,
  because regenerating reasoning traces after a *classification* failure
  would be incoherent.  The topology guard still constrains node_6 to
  {node_1, node_4, node_8}.

Cross-references: docs/PIPELINE.md (node-by-node contract),
docs/ARCHITECTURE.md (why topology-is-enforcement),
case_engine/pipeline/state_machine.py (transition guard).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from case_engine.escalation import EscalationHandoff
from case_engine.exceptions import CASEError, LLMAdapterError, RoutingError
from case_engine.llm.base import ToolCallRequest
from case_engine.logging_setup import get_logger
from case_engine.observability import safe_incr, safe_observe
from case_engine.registry.models import MentalModelDefinition
from case_engine.schemas.pipeline import AuditEventType, PipelinePhase
from case_engine.schemas.reports import TraceRecord

if TYPE_CHECKING:  # pragma: no cover - type-checking only import
    from case_engine.pipeline.context import EngineContext

__all__ = [
    "NODES", "ENTRY_NODE", "END_NODE",
    "node_1_intent_classifier", "node_2_model_router", "node_3_schema_injection",
    "node_4_llm_generation", "node_5_ast_validator", "node_6_reflexion_loop",
    "node_7_audit_and_synthesis", "node_8_human_escalation",
]

log = get_logger(__name__)

ENTRY_NODE = "node_1_intent_classifier"
END_NODE = "END"

_CLASSIFIER_SYSTEM_PROMPT = (
    "You are the intent-classification node of the C.A.S.E. reasoning engine.\n"
    "Classify the user's problem into exactly one Cynefin domain.\n"
    "You MUST call the `{tool}` tool with every field populated. Free-text "
    "answers are rejected by the engine."
)

_MODEL_SYSTEM_PROMPT = (
    "You are executing the mental model '{name}' ({mid}) inside the C.A.S.E. "
    "reasoning engine.\n"
    "Model contract: {description}\n"
    "You MUST call the `{tool}` tool with EVERY field populated and "
    "substantive. Free-text answers are rejected by the engine."
)


def _copy(state: dict) -> dict:
    """Return a shallow copy of *state* (nodes never mutate their input).

    Nested dicts (traces, errors) are replaced wholesale, never mutated,
    so a shallow copy is sufficient and cheap.  This copy-on-write
    discipline is what lets the same nodes run safely under both the
    pure-Python machine and the LangGraph adapter (last-value-wins
    channel merging).

    Args:
        state: Current pipeline state.

    Returns:
        Copied state dict.
    """
    log.debug("state copied", extra={"state_keys": len(state)})
    return dict(state)


def _truncate(value: Any, limit: int = 120) -> str:
    """Return ``str(value)`` collapsed to one line and truncated to *limit*.

    Log-safety helper: prompts and completions must never reach the logs
    in full above DEBUG level, so node entry records carry only a
    truncated summary of the user query.

    Args:
        value: Value to preview; non-strings are ``str()``-rendered.
        limit: Maximum characters before the ellipsis is appended.

    Returns:
        Single-line preview, at most ``limit`` characters plus an ellipsis.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _payload_hash(payload: dict[str, Any]) -> str:
    """Return the SHA-256 of a payload's canonical JSON (audit aid).

    Used so audit events can reference a full payload without ever
    embedding it; the first 16 characters are also emitted at DEBUG.

    Args:
        payload: JSON-safe dict.

    Returns:
        Hex digest (first 16 chars of full digest are logged upstream).

    Example:
        >>> _payload_hash({"a": 1}) == _payload_hash({"a": 1})
        True
    """
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    log.debug("payload hashed",
              extra={"payload_hash": digest[:16], "payload_keys": sorted(payload)})
    return digest


def _user_prompt(state: dict, feedback: str | None) -> str:
    """Compose the user prompt: problem, optional context, optional feedback.

    The prompt is the *content* half of the engine's forcing function (the
    LLM may only fill tool-call payloads); its text is deliberately never
    logged in full — only its shape is emitted at DEBUG.

    Args:
        state: Current pipeline state (reads ``user_query`` and ``context``).
        feedback: Prior validation feedback for this model, if any
            (present exactly when this is a reflexion retry).

    Returns:
        The composed prompt string.

    Example:
        >>> _user_prompt({"user_query": "hi"}, None)  # doctest: +SKIP
        'PROBLEM:\\nhi'
    """
    parts = [f"PROBLEM:\n{state['user_query']}"]
    context = state.get("context") or {}
    if context:
        parts.append("CONTEXT:\n" + json.dumps(context, ensure_ascii=False, default=str))
    if feedback:
        parts.append(
            "PREVIOUS ATTEMPT REJECTED BY THE ENFORCEMENT ENGINE.\n"
            f"VALIDATION FEEDBACK (fix every point):\n{feedback}"
        )
    log.debug(
        "prompt composed",
        extra={"query_chars": len(state.get("user_query") or ""),
               "context_keys": sorted(context),
               "reflexion_feedback": bool(feedback)},
    )
    return "\n\n".join(parts)


def _record_usage(state: dict, call: Any) -> None:
    """Accumulate one LLM call's token usage into the session state (G7).

    Reads the provider-reported usage dict from a
    :class:`~case_engine.llm.base.ToolCallResult` and merges it into the
    JSON-safe ``state["llm_usage"]`` accumulator
    (``{calls, prompt_tokens, completion_tokens, total_tokens}``).
    Providers that report no usage contribute a call count but zero
    tokens, so ``calls`` is an honest upper-bound metric even with
    tight-fisted providers.  Counts only — token *content* never enters
    state, logs or the ledger.

    Args:
        state: Current pipeline state (mutated in place on the **copy**
            the calling node already made — never the caller's input).
        call: The completed :class:`ToolCallResult`.
    """
    usage = getattr(call, "usage", None) or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    agg = dict(state.get("llm_usage") or {
        "calls": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "total_tokens": 0,
    })
    agg["calls"] = int(agg.get("calls", 0)) + 1
    agg["prompt_tokens"] = int(agg.get("prompt_tokens", 0)) + max(0, prompt)
    agg["completion_tokens"] = int(agg.get("completion_tokens", 0)) + max(0, completion)
    agg["total_tokens"] = agg["prompt_tokens"] + agg["completion_tokens"]
    state["llm_usage"] = agg
    log.debug(
        "llm usage recorded",
        extra={"calls": agg["calls"], "prompt_tokens": agg["prompt_tokens"],
               "completion_tokens": agg["completion_tokens"],
               "provider_reported": bool(usage)},
    )


def _usage_event_payload(state: dict) -> dict[str, int]:
    """Return the JSON-safe LLM_USAGE payload for the terminal nodes.

    Args:
        state: Current pipeline state.

    Returns:
        ``{calls, prompt_tokens, completion_tokens, total_tokens}`` with
        a defensive fallback to the zero-usage shape.
    """
    agg = state.get("llm_usage") or {}
    payload = {
        "calls": int(agg.get("calls", 0) or 0),
        "prompt_tokens": int(agg.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(agg.get("completion_tokens", 0) or 0),
    }
    payload["total_tokens"] = payload["prompt_tokens"] + payload["completion_tokens"]
    return payload


def _trace_record(model_def: MentalModelDefinition, payload: dict, attempts: int) -> dict:
    """Build a serialized TraceRecord for pipeline state.

    The record deliberately retains even rejected payloads so the audit
    trail shows every attempt, not only the accepted one.

    Args:
        model_def: Owning model definition.
        payload: (Un)validated payload dict.
        attempts: Generation attempts so far.

    Returns:
        TraceRecord-ready dict.
    """
    log.debug(
        "trace record built",
        extra={"model_id": model_def.algorithm_id, "attempts": attempts},
    )
    return TraceRecord(
        model_id=model_def.algorithm_id,
        model_name=model_def.algorithm_name,
        tool_name=model_def.tool_name,
        payload=payload,
        attempts=attempts,
    ).model_dump()


# --------------------------------------------------------------------------- #
# node_1 — classify_cynefin_domain
# --------------------------------------------------------------------------- #
def node_1_intent_classifier(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Classify the query into a Cynefin domain (spec ``node_1``).

    Implements the spec's ``classify_cynefin_domain`` step, plus the G1
    PII/TEE gate.  Fast path: when the caller preset
    ``domain_classification`` (``--domain``), the LLM is skipped and the
    preset is audited as ``DOMAIN_PRESET``.  Otherwise a single forced
    tool call to the MM013 classifier is generated; the payload must
    clear the schema gate *and* the full validation battery (VR013,
    VR016) before it is accepted.

    G1 PII/TEE gate (runs FIRST, before classification and before any
    generation): the configured :class:`~case_engine.privacy.PIIDetector`
    scans the user query and operator context.  When PII is found and
    ``guardrails.tee_required_for_pii`` is set without TEE attestation,
    the session follows ``guardrails.pii_policy``: ``escalate`` routes
    straight to node_8 (fail-closed — no LLM generation ever sees the
    PII), ``allow_with_flags`` proceeds with the session flagged.  Every
    outcome is recorded in a ``TEE_DECISION`` audit event carrying only
    category names — never the raw values.

    State inputs: ``user_query``, ``context``, optional preset
    ``domain_classification``, ``tee_attested``.
    State outputs: ``current_node``, ``phase``, ``domain_classification``,
    ``domain_source`` (``"preset"`` | ``"classified"``),
    ``reasoning_traces`` (MM013 record retained), ``pii_detected``,
    ``pii_categories``, ``tee_decision``, and on failure
    ``validation_errors`` plus ``reflexion_target``.

    Audit events: ``TEE_DECISION`` (when PII was detected),
    ``DOMAIN_PRESET`` (fast path) or ``DOMAIN_CLASSIFIED`` (success);
    ``TRACE_REJECTED`` (gate) / ``VALIDATION_FAILED`` (adapter or rules)
    on failure.

    Failure/rollback semantics: any adapter error, gate rejection or
    validation failure records feedback in ``validation_errors`` and routes
    to node_6 with ``reflexion_target`` set back to *this* node — reflexion
    regenerates the classification itself, not the downstream generation.
    The PII-escalation path is NOT a reflexion failure: it terminates the
    session deliberately (new ``pii_escalation`` edge), because no
    regeneration can cure a policy refusal.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry, LLM client, gate, validator, ledger,
            PII detector).

    Returns:
        ``(next_node, state)`` — ``node_2_model_router`` on success,
        ``node_6_reflexion_loop`` on generation/validation failure, or
        ``node_8_human_escalation`` on a PII policy refusal (G1).

    Raises:
        LedgerError: Propagated from ``ctx.audit`` — audit is fail-closed,
            so an unwritable ledger halts the run rather than continuing
            unaudited.

    Example:
        >>> node_1_intent_classifier(state, ctx)  # doctest: +SKIP
        ('node_2_model_router', {...})
    """
    node_id = "node_1_intent_classifier"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.INTENT_CLASSIFICATION.value
    session_id = state["session_id"]
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "query_preview": _truncate(state.get("user_query")),
               "preset_domain": state.get("domain_classification")},
    )

    # ---- G1: PII/TEE gate (before classification and generation) ------- #
    scan_text = state.get("user_query") or ""
    context_blob = state.get("context") or {}
    if context_blob:
        scan_text = f"{scan_text}\n{json.dumps(context_blob, ensure_ascii=False, default=str)}"
    finding = ctx.pii_detector.detect(scan_text)
    state["pii_detected"] = finding.detected
    state["pii_categories"] = list(finding.categories)
    state["tee_attested"] = bool(state.get("tee_attested"))
    if finding.detected:
        log.warning(
            "PII detected in session input",
            extra={"node": node_id, "session_id": session_id,
                   "pii_categories": finding.categories,
                   "tee_attested": state["tee_attested"],
                   "pii_policy": ctx.guardrails.pii_policy},
        )
    if finding.detected and ctx.guardrails.tee_required_for_pii:
        if state["tee_attested"]:
            state["tee_decision"] = "tee_attested"
            ctx.audit(AuditEventType.TEE_DECISION, session_id, node_id,
                      {"decision": "tee_attested",
                       "pii_categories": finding.categories})
        elif ctx.guardrails.pii_policy == "escalate":
            # Fail-closed refusal: no LLM generation ever sees this input.
            state["tee_decision"] = "escalated"
            state["error_detail"] = {
                "reason": "pii_detected_without_tee",
                "pii_categories": finding.categories,
                "policy": "escalate",
            }
            ctx.audit(AuditEventType.TEE_DECISION, session_id, node_id,
                      {"decision": "escalated",
                       "pii_categories": finding.categories,
                       "policy": "escalate",
                       "action": "session refused before generation"})
            log.warning(
                "PII without TEE attestation - session refused (pii_policy=escalate)",
                extra={"node": node_id, "session_id": session_id,
                       "pii_categories": finding.categories},
            )
            return "node_8_human_escalation", state
        else:  # allow_with_flags
            state["tee_decision"] = "allow_with_flags"
            ctx.audit(AuditEventType.TEE_DECISION, session_id, node_id,
                      {"decision": "allow_with_flags",
                       "pii_categories": finding.categories,
                       "policy": "allow_with_flags"})
            log.warning(
                "PII without TEE attestation - proceeding flagged "
                "(pii_policy=allow_with_flags)",
                extra={"node": node_id, "session_id": session_id,
                       "pii_categories": finding.categories},
            )
    elif finding.detected:
        state["tee_decision"] = "policy_disabled"
        ctx.audit(AuditEventType.TEE_DECISION, session_id, node_id,
                  {"decision": "policy_disabled",
                   "pii_categories": finding.categories})

    if state.get("domain_classification"):
        log.debug(
            "decision: preset-domain fast path (LLM classification skipped)",
            extra={"node": node_id, "session_id": session_id,
                   "domain": state["domain_classification"]},
        )
        state["domain_source"] = "preset"
        ctx.audit(AuditEventType.DOMAIN_PRESET, session_id, node_id,
                  {"domain": state["domain_classification"]})
        log.info(
            "node completed",
            extra={"node": node_id, "session_id": session_id,
                   "outcome": "preset domain accepted",
                   "domain": state["domain_classification"]},
        )
        return "node_2_model_router", state

    model_def = ctx.registry.get("MM013")
    spec = ctx.tool_factory.openai_tool(model_def)
    request = ToolCallRequest(
        system_prompt=_CLASSIFIER_SYSTEM_PROMPT.format(tool=model_def.tool_name),
        user_prompt=_user_prompt(state, None),
        tool_specs=[spec],
        tool_choice="required",
        max_tokens=ctx.guardrails.max_reasoning_tokens,
        metadata={"session_id": session_id, "node": node_id},
    )
    try:
        call = ctx.llm.generate_tool_call(request)
    except LLMAdapterError as exc:
        log.error(
            "LLM adapter failure during classification",
            extra={"node": node_id, "session_id": session_id,
                   "model": "MM013", "error": str(exc)},
        )
        safe_incr(ctx.metrics, "case_engine_llm_adapter_failures_total",
                  node=node_id, model="MM013")
        state["validation_errors"] = {
            "MM013": f"LLM adapter failure during classification: {exc}"
        }
        state["reflexion_target"] = node_id
        ctx.audit(AuditEventType.VALIDATION_FAILED, session_id, node_id,
                  {"stage": "classification", "error": str(exc)})
        log.debug(
            "decision: failure edge -> reflexion (regenerate classification)",
            extra={"node": node_id, "session_id": session_id,
                   "reflexion_target": node_id},
        )
        return "node_6_reflexion_loop", state

    _record_usage(state, call)
    safe_observe(ctx.metrics, "case_engine_llm_latency_ms", call.latency_ms,
                 node=node_id, model="MM013")

    decision = ctx.gate.check(call.tool_name, call.arguments)
    if not decision.approved:
        log.warning(
            "gate rejected classifier payload",
            extra={"node": node_id, "session_id": session_id,
                   "model": "MM013", "errors": decision.errors[:3]},
        )
        state["validation_errors"] = {
            "MM013": "Classifier payload rejected: " + "; ".join(decision.errors[:5])
        }
        state["reflexion_target"] = node_id
        ctx.audit(AuditEventType.TRACE_REJECTED, session_id, node_id,
                  {"tool": call.tool_name, "errors": decision.errors[:5]})
        log.debug(
            "decision: failure edge -> reflexion (regenerate classification)",
            extra={"node": node_id, "session_id": session_id,
                   "reflexion_target": node_id},
        )
        return "node_6_reflexion_loop", state

    parsed = decision.parsed or {}
    report = ctx.validator.validate(model_def, parsed)
    traces = dict(state.get("reasoning_traces") or {})
    previous = traces.get("MM013")
    attempts = (previous.get("attempts", 0) if isinstance(previous, dict) else 0) + 1
    record = _trace_record(model_def, parsed, attempts)
    record["report"] = report.model_dump()
    traces["MM013"] = record
    state["reasoning_traces"] = traces

    if not report.passed:
        feedback = "; ".join(
            f"{r.rule_id}: {r.message}" for r in report.rule_reports if not r.passed
        )
        log.warning(
            "validation rejected classifier payload; entering reflexion",
            extra={"node": node_id, "session_id": session_id, "model": "MM013",
                   "attempt": attempts, "feedback_preview": _truncate(feedback)},
        )
        state["validation_errors"] = {"MM013": feedback}
        state["reflexion_target"] = node_id
        ctx.audit(AuditEventType.VALIDATION_FAILED, session_id, node_id,
                  {"model": "MM013", "feedback": feedback[:400]})
        log.debug(
            "decision: failure edge -> reflexion (regenerate classification)",
            extra={"node": node_id, "session_id": session_id,
                   "reflexion_target": node_id},
        )
        return "node_6_reflexion_loop", state

    state["domain_classification"] = parsed.get("assigned_domain")
    state["domain_source"] = "classified"
    state["validation_errors"] = {}
    ctx.audit(AuditEventType.DOMAIN_CLASSIFIED, session_id, node_id,
              {"domain": parsed.get("assigned_domain"),
               "confidence": parsed.get("confidence")})
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "domain classified",
               "domain": state["domain_classification"]},
    )
    return "node_2_model_router", state


# --------------------------------------------------------------------------- #
# node_2 — select_cognitive_models
# --------------------------------------------------------------------------- #
def node_2_model_router(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Route the classified domain to mental models (spec ``node_2``).

    Implements the spec's ``select_cognitive_models`` step.  Deterministic:
    ranking by ``domain_affinity`` with spec-priority tie-breaks; the LLM
    has no say.  Operator overrides (``forced_model_ids``) bypass ranking
    and are audited as such.

    State inputs: ``domain_classification``, optional ``forced_model_ids``.
    State outputs: ``current_node``, ``phase``, ``selected_model_ids`` (and
    ``error_detail`` on the escalation paths).

    Audit events: ``MODELS_SELECTED`` (success, includes the affinity score
    table) or ``SESSION_FAILED`` (unroutable domain).

    Failure semantics: routing failures escalate straight to node_8 — there
    is no reflexion for routing, because no regeneration can fix an unknown
    domain or an unresolvable forced-model id.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry-backed router, ledger).

    Returns:
        ``(next_node, state)`` — ``node_3_schema_injection`` on success,
        ``node_8_human_escalation`` on routing failure.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).

    Example:
        >>> nxt, st = node_2_model_router(state, ctx)  # doctest: +SKIP
        >>> st["selected_model_ids"]  # doctest: +SKIP
        ['MM001', 'MM002', 'MM003']
    """
    node_id = "node_2_model_router"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.MODEL_ROUTING.value
    session_id = state["session_id"]
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "domain": state.get("domain_classification"),
               "forced_model_ids": state.get("forced_model_ids")},
    )

    domain = state.get("domain_classification")
    if not domain:
        # Topologically unreachable (node_1 gates this); defensive escalate.
        log.warning(
            "entered without a domain; defensive escalation",
            extra={"node": node_id, "session_id": session_id},
        )
        state["error_detail"] = {"reason": "node_2 entered without a domain"}
        ctx.audit(AuditEventType.SESSION_FAILED, session_id, node_id,
                  {"reason": "missing domain classification"})
        return "node_8_human_escalation", state

    try:
        selected = ctx.router.select(
            domain, top_n=3, forced_model_ids=state.get("forced_model_ids")
        )
    except RoutingError as exc:
        log.error(
            "routing failure — escalating",
            extra={"node": node_id, "session_id": session_id,
                   "domain": domain, "error": str(exc)},
        )
        state["error_detail"] = {"reason": str(exc), "domain": domain}
        ctx.audit(AuditEventType.SESSION_FAILED, session_id, node_id,
                  {"reason": str(exc), "domain": domain})
        return "node_8_human_escalation", state

    state["selected_model_ids"] = [m.algorithm_id for m in selected]
    ctx.audit(AuditEventType.MODELS_SELECTED, session_id, node_id,
              {"domain": domain,
               "models": state["selected_model_ids"],
               "affinity": ctx.router.explain(domain)})
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "models selected", "domain": domain,
               "models": state["selected_model_ids"]},
    )
    return "node_3_schema_injection", state


# --------------------------------------------------------------------------- #
# node_3 — inject_pydantic_schemas
# --------------------------------------------------------------------------- #
def node_3_schema_injection(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Register the selected contracts as tool specs (spec ``node_3``).

    Implements the spec's ``inject_pydantic_schemas`` step.  Builds an
    OpenAI-shaped tool spec per selected model from its typed Pydantic
    contract via :class:`~case_engine.enforcement.tool_factory.ToolFactory`;
    ``tool_choice="required"`` semantics are declared so the generation
    node's calls cannot fall back to free text.

    State inputs: ``selected_model_ids``.
    State outputs: ``current_node``, ``phase``, ``tool_specs`` (and
    ``error_detail`` on failure).

    Audit events: ``SCHEMAS_INJECTED`` (tool names) or ``SESSION_FAILED``.

    Failure semantics: an unresolvable registry id or contract/schema drift
    is a deploy-time bug, not a retryable rejection — the node escalates
    straight to node_8 with the offending reason recorded.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry, tool factory, ledger).

    Returns:
        ``(next_node, state)`` — ``node_4_llm_generation`` on success,
        ``node_8_human_escalation`` if a selected id cannot resolve.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).

    Example:
        >>> nxt, st = node_3_schema_injection(state, ctx)  # doctest: +SKIP
        >>> [s["function"]["name"] for s in st["tool_specs"]]  # doctest: +SKIP
        ['complexity_domain_classifier', ...]
    """
    node_id = "node_3_schema_injection"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.SCHEMA_INJECTION.value
    session_id = state["session_id"]
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "selected_model_ids": state.get("selected_model_ids")},
    )

    specs: list[dict] = []
    try:
        for model_id in state.get("selected_model_ids") or []:
            model_def = ctx.registry.get(model_id)
            specs.append(ctx.tool_factory.openai_tool(model_def))
    except Exception as exc:  # RegistryError family -> escalate
        log.error(
            "schema injection failure — escalating",
            extra={"node": node_id, "session_id": session_id, "error": str(exc)},
        )
        state["error_detail"] = {"reason": f"Schema injection failed: {exc}"}
        ctx.audit(AuditEventType.SESSION_FAILED, session_id, node_id,
                  {"reason": str(exc)})
        return "node_8_human_escalation", state

    state["tool_specs"] = specs
    ctx.audit(AuditEventType.SCHEMAS_INJECTED, session_id, node_id,
              {"tools": [s["function"]["name"] for s in specs],
               "tool_choice": "required"})
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "tool specs injected", "tools": len(specs)},
    )
    return "node_4_llm_generation", state


# --------------------------------------------------------------------------- #
# node_4 — generate_reasoning_trace (schema-gated, per selected model)
# --------------------------------------------------------------------------- #
def node_4_llm_generation(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Generate one gated tool call per selected model (spec ``node_4``).

    Implements the spec's ``generate_reasoning_trace`` step.  For every
    selected model the node composes a model-specific prompt (including
    reflexion feedback, if any) and forces a single tool call; the payload
    is stored only after the :class:`~case_engine.enforcement.gate.SchemaGate`
    approves it.  On ANY failure (adapter error or gate rejection) the node
    aborts the round, records feedback and routes to the reflexion loop
    (``schema_violation`` edge) with ``reflexion_target`` set back to this
    node — all-or-nothing per round, mirroring the spec's round semantics.

    State inputs: ``tool_specs``, ``user_query``, ``context``,
    ``selected_model_ids``, ``validation_errors`` (reflexion feedback),
    ``reasoning_traces`` (for attempt counting).
    State outputs: ``current_node``, ``phase``, ``reasoning_traces`` updated
    (attempts counted per model), ``validation_errors`` (cleared on success
    — node_5 runs a fresh validation round), ``reflexion_target`` on
    failure.

    Audit events: ``TRACE_GENERATED`` per accepted trace (when
    ``guardrails.audit_every_trace``), ``TRACE_REJECTED`` with the gate
    errors, ``VALIDATION_FAILED`` on adapter errors.

    Failure/rollback semantics: the first failure ends the round; reflexion
    re-enters *this* node with the recorded feedback, and budget exhaustion
    eventually escalates to node_8 with the partial traces retained.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry, LLM client, gate, ledger, guardrails).

    Returns:
        ``(next_node, state)`` — ``node_5_ast_validator`` on success,
        ``node_6_reflexion_loop`` on any violation.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).
        RegistryError: If a selected model id cannot be resolved (deploy-
            time bug; node_3 already validated the ids, so this is
            defensive).

    Example:
        >>> node_4_llm_generation(state, ctx)  # doctest: +SKIP
        ('node_5_ast_validator', {...})
    """
    node_id = "node_4_llm_generation"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.GENERATION.value
    session_id = state["session_id"]
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "models": state.get("selected_model_ids"),
               "query_preview": _truncate(state.get("user_query")),
               "retry_count": state.get("retry_count", 0)},
    )

    traces = dict(state.get("reasoning_traces") or {})
    feedback_map = dict(state.get("validation_errors") or {})
    specs_by_tool = {s["function"]["name"]: s for s in state.get("tool_specs") or []}

    for model_id in state.get("selected_model_ids") or []:
        model_def = ctx.registry.get(model_id)
        spec = specs_by_tool.get(model_def.tool_name)
        if spec is None:  # unreachable: node_3 injects all; defensive
            log.warning(
                "no injected tool spec for model; entering reflexion",
                extra={"node": node_id, "session_id": session_id,
                       "model_id": model_id},
            )
            state["validation_errors"] = {model_id: "No injected tool spec"}
            state["reflexion_target"] = node_id
            return "node_6_reflexion_loop", state

        feedback = feedback_map.get(model_id)
        log.debug(
            "generating gated tool call",
            extra={"node": node_id, "session_id": session_id,
                   "model_id": model_id, "with_feedback": feedback is not None},
        )
        request = ToolCallRequest(
            system_prompt=_MODEL_SYSTEM_PROMPT.format(
                name=model_def.algorithm_name, mid=model_id,
                description=model_def.description_for_llm, tool=model_def.tool_name,
            ),
            user_prompt=_user_prompt(state, feedback),
            tool_specs=[spec],
            tool_choice="required",
            max_tokens=ctx.guardrails.max_reasoning_tokens,
            metadata={"session_id": session_id, "node": node_id, "model_id": model_id},
        )
        try:
            call = ctx.llm.generate_tool_call(request)
        except LLMAdapterError as exc:
            log.error(
                "LLM adapter failure during generation",
                extra={"node": node_id, "session_id": session_id,
                       "model_id": model_id, "error": str(exc)},
            )
            safe_incr(ctx.metrics, "case_engine_llm_adapter_failures_total",
                      node=node_id, model=model_id)
            feedback_map[model_id] = f"LLM adapter failure: {exc}"
            state["validation_errors"] = feedback_map
            state["reflexion_target"] = node_id
            ctx.audit(AuditEventType.VALIDATION_FAILED, session_id, node_id,
                      {"model": model_id, "error": str(exc)})
            log.debug(
                "decision: failure edge -> reflexion (regenerate traces)",
                extra={"node": node_id, "session_id": session_id,
                       "reflexion_target": node_id},
            )
            return "node_6_reflexion_loop", state

        _record_usage(state, call)
        safe_observe(ctx.metrics, "case_engine_llm_latency_ms", call.latency_ms,
                     node=node_id, model=model_id)

        decision = ctx.gate.check(call.tool_name, call.arguments)
        previous = traces.get(model_id) if isinstance(traces.get(model_id), dict) else None
        attempts = (previous.get("attempts", 0) if previous else 0) + 1

        if not decision.approved:
            log.warning(
                "gate rejected payload; entering reflexion",
                extra={"node": node_id, "session_id": session_id,
                       "model_id": model_id, "attempt": attempts,
                       "errors": decision.errors[:3]},
            )
            record = _trace_record(model_def, call.arguments, attempts)
            record["report"] = None
            traces[model_id] = record
            state["reasoning_traces"] = traces
            feedback_map[model_id] = (
                "Schema gate rejected the payload: " + "; ".join(decision.errors[:5])
            )
            state["validation_errors"] = feedback_map
            state["reflexion_target"] = node_id
            ctx.audit(AuditEventType.TRACE_REJECTED, session_id, node_id,
                      {"model": model_id, "tool": call.tool_name,
                       "errors": decision.errors[:5], "attempt": attempts})
            log.debug(
                "decision: failure edge -> reflexion (regenerate traces)",
                extra={"node": node_id, "session_id": session_id,
                       "reflexion_target": node_id},
            )
            return "node_6_reflexion_loop", state

        record = _trace_record(model_def, decision.parsed or {}, attempts)
        traces[model_id] = record
        state["reasoning_traces"] = traces
        if ctx.guardrails.audit_every_trace:
            ctx.audit(AuditEventType.TRACE_GENERATED, session_id, node_id,
                      {"model": model_id, "attempt": attempts,
                       "payload_hash": _payload_hash(decision.parsed or {})})
        log.info(
            "trace generated",
            extra={"node": node_id, "session_id": session_id,
                   "model_id": model_id, "attempt": attempts},
        )
        safe_incr(ctx.metrics, "case_engine_traces_generated_total",
                  model=model_id, attempt="retry" if attempts > 1 else "first")

    state["validation_errors"] = {}  # fresh validation round at node_5
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "generation round complete",
               "models": state.get("selected_model_ids")},
    )
    return "node_5_ast_validator", state


# --------------------------------------------------------------------------- #
# node_5 — validate_against_registry
# --------------------------------------------------------------------------- #
def node_5_ast_validator(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Validate every trace: Pydantic parse + rule battery (spec ``node_5``).

    Implements the spec's ``validate_against_registry`` step.  Validates
    ALL retained traces (including the node_1 classifier trace), which is
    stronger than the spec's per-round validation and keeps the audit
    trail complete.  Rules never run on a schema-invalid trace (the gate
    already rejected those before a record was kept).

    State inputs: ``reasoning_traces``.
    State outputs: ``current_node``, ``phase``, ``reasoning_traces`` (each
    record's ``report`` filled in), ``all_valid``, ``validation_errors``
    (per-model feedback for the next generation round), and on failure
    ``reflexion_target``.

    Audit events: ``TRACE_VALIDATED`` or ``VALIDATION_FAILED`` (with failed
    rule ids) per trace.

    Failure/rollback semantics: any invalid trace sets
    ``reflexion_target = node_4_llm_generation`` (the failing stage for
    trace content) and routes to node_6; the feedback map is what the next
    generation round must fix.  Budget exhaustion then escalates to node_8
    with the partial traces preserved.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry, validator, ledger).

    Returns:
        ``(next_node, state)`` — ``node_7_audit_and_synthesis`` when
        ``all_valid`` else ``node_6_reflexion_loop``.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).
        RegistryError: If a trace's model id is no longer resolvable
            (deploy-time registry change mid-session; defensive).

    Example:
        >>> node_5_ast_validator(state, ctx)[1]["all_valid"]  # doctest: +SKIP
        True
    """
    node_id = "node_5_ast_validator"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.VALIDATION.value
    session_id = state["session_id"]
    traces_pre = dict(state.get("reasoning_traces") or {})
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "trace_ids": sorted(traces_pre)},
    )

    traces = traces_pre
    all_valid = True
    errors: dict[str, str] = {}

    for model_id, record in traces.items():
        log.debug(
            "validating trace",
            extra={"node": node_id, "session_id": session_id,
                   "model_id": model_id},
        )
        model_def = ctx.registry.get(model_id)
        report = ctx.validator.validate(model_def, record.get("payload") or {})
        record = dict(record)
        record["report"] = report.model_dump()
        traces[model_id] = record
        if report.passed:
            ctx.audit(AuditEventType.TRACE_VALIDATED, session_id, node_id,
                      {"model": model_id, "rules": [r.rule_id for r in report.rule_reports]})
            continue
        all_valid = False
        failed = [r for r in report.rule_reports if not r.passed]
        errors[model_id] = "; ".join(f"{r.rule_id}: {r.message}" for r in failed)
        ctx.audit(AuditEventType.VALIDATION_FAILED, session_id, node_id,
                  {"model": model_id,
                   "failed_rules": [r.rule_id for r in failed],
                   "feedback": errors[model_id][:600]})

    state["reasoning_traces"] = traces
    state["all_valid"] = all_valid
    state["validation_errors"] = errors
    if all_valid:
        log.info(
            "node completed",
            extra={"node": node_id, "session_id": session_id,
                   "outcome": "all traces valid", "all_valid": True},
        )
        return "node_7_audit_and_synthesis", state
    state["reflexion_target"] = "node_4_llm_generation"
    log.warning(
        "validation failed; entering reflexion",
        extra={"node": node_id, "session_id": session_id,
               "failed_models": sorted(errors),
               "reflexion_target": state["reflexion_target"]},
    )
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "invalid traces -> reflexion", "all_valid": False},
    )
    return "node_6_reflexion_loop", state


# --------------------------------------------------------------------------- #
# node_6 — reject_and_retry (reflexion)
# --------------------------------------------------------------------------- #
def node_6_reflexion_loop(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Append feedback, count the retry, route back or escalate (spec ``node_6``).

    Implements the spec's ``reject_and_retry`` step.  Semantics:
    ``retry_count`` is incremented on entry; at most
    ``max_reflexion_retries`` regeneration cycles run after the initial
    attempt (e.g. 3 -> up to 4 generation rounds total), then the session
    escalates (fail-closed).

    State inputs: ``validation_errors`` (the feedback map),
    ``reflexion_target`` (the node that failed), ``retry_count``.
    State outputs: ``current_node``, ``phase``, ``retry_count``.

    Audit events: ``REFLEXION_RETRY`` (retry_count + full error map).

    Failure/rollback semantics: the loop returns to the recorded
    ``reflexion_target`` — the node that actually failed (node_1 for
    classification failures, node_4 for generation/validation failures) —
    *not* blindly to node_4.  When the budget is exhausted the session
    follows ``guardrails.fallback_behavior`` (G2): the default
    ``escalate_to_human_with_partial_trace`` routes to node_8, which
    packages the partial traces accumulated so far for a human reviewer;
    ``abort_session`` routes to node_8 which closes the session as
    aborted (no escalation is claimed); ``passthrough_with_flags``
    proceeds to node_7 with the failed traces loudly flagged (a
    ``FALLBACK_APPLIED`` audit event and a WARNING always accompany it —
    passthrough is never silent); ``raise`` (an infrastructure policy)
    is treated as the default here — reasoning failures are data, not
    exceptions.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (guardrails for the retry cap, ledger).

    Returns:
        ``(next_node, state)`` — the recorded ``reflexion_target``
        (node_1 or node_4) while budget remains; on exhaustion, node_8
        (escalate/abort) or node_7 (``passthrough_with_flags``).

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).

    Example:
        >>> nxt, st = node_6_reflexion_loop(state, ctx)  # doctest: +SKIP
        >>> nxt in ("node_1_intent_classifier", "node_4_llm_generation",  # doctest: +SKIP
        ...         "node_8_human_escalation")
        True
    """
    node_id = "node_6_reflexion_loop"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.REFLEXION.value
    session_id = state["session_id"]

    state["retry_count"] = int(state.get("retry_count") or 0) + 1
    retry_count = state["retry_count"]
    log.warning(
        "validation rejections entering reflexion",
        extra={"node": node_id, "session_id": session_id,
               "retry_count": retry_count,
               "errors": list((state.get("validation_errors") or {}).keys())},
    )
    ctx.audit(AuditEventType.REFLEXION_RETRY, session_id, node_id,
              {"retry_count": retry_count,
               "errors": state.get("validation_errors") or {}})
    safe_incr(ctx.metrics, "case_engine_reflexion_retries_total",
              target=state.get("reflexion_target") or "node_4_llm_generation")
    log.debug(
        "reflexion budget check",
        extra={"node": node_id, "session_id": session_id,
               "retry_count": retry_count,
               "max_reflexion_retries": ctx.guardrails.max_reflexion_retries,
               "budget_remaining": ctx.guardrails.max_reflexion_retries - retry_count},
    )

    if retry_count <= ctx.guardrails.max_reflexion_retries:
        target = state.get("reflexion_target") or "node_4_llm_generation"
        log.info(
            "reflexion fallback chosen",
            extra={"node": node_id, "session_id": session_id,
                   "target": target, "retry_count": retry_count},
        )
        return target, state

    # ---- G2: budget exhausted — apply the configured fallback policy ---- #
    behavior = ctx.guardrails.fallback_behavior
    failure_summary = {
        "reason": "reflexion_budget_exhausted",
        "policy": behavior,
        "retry_count": retry_count,
        "failed_models": sorted(state.get("validation_errors") or {}),
    }
    if behavior == "passthrough_with_flags":
        # Never silent: loud WARNING + FALLBACK_APPLIED event, then node_7
        # synthesizes with the failed traces flagged in the result.
        log.warning(
            "reflexion budget exhausted - PASSTHROUGH WITH FLAGS applied "
            "(unvalidated traces will be synthesized and flagged)",
            extra={"node": node_id, "session_id": session_id,
                   "retry_count": retry_count,
                   "partial_traces": sorted(state.get("reasoning_traces") or {})},
        )
        ctx.audit(AuditEventType.FALLBACK_APPLIED, session_id, node_id,
                  {**failure_summary, "action": "proceeding to synthesis with flags"})
        state["passthrough_accepted"] = True
        return "node_7_audit_and_synthesis", state
    if behavior == "abort_session":
        log.warning(
            "reflexion budget exhausted - session aborted per "
            "fallback_behavior=abort_session",
            extra={"node": node_id, "session_id": session_id,
                   "retry_count": retry_count},
        )
        ctx.audit(AuditEventType.FALLBACK_APPLIED, session_id, node_id,
                  {**failure_summary, "action": "session aborted (no escalation claimed)"})
        state["abort_requested"] = True
        return "node_8_human_escalation", state
    log.warning(
        "reflexion budget exhausted — escalating",
        extra={"node": node_id, "session_id": session_id,
               "retry_count": retry_count,
               "max_reflexion_retries": ctx.guardrails.max_reflexion_retries,
               "partial_traces": sorted(state.get("reasoning_traces") or {})},
    )
    return "node_8_human_escalation", state


# --------------------------------------------------------------------------- #
# node_7 — record_trace_and_respond
# --------------------------------------------------------------------------- #
def _summarize_trace(model_def: MentalModelDefinition, record: dict) -> str:
    """Render one trace's ``summary_fields`` for the final response.

    Pure formatting helper used by node_7; missing/empty fields are
    skipped, and a trace with nothing populated yields a placeholder so
    the synthesis never emits blank model sections.

    Args:
        model_def: Owning model definition.
        record: Serialized TraceRecord.

    Returns:
        Human-readable summary line(s).
    """
    payload = record.get("payload") or {}
    parts: list[str] = []
    for field_name in model_def.summary_fields:
        value = payload.get(field_name)
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value)
        if value is not None and str(value).strip():
            parts.append(f"{field_name}: {value}")
    summary = " | ".join(parts) if parts else "(no summary fields populated)"
    log.debug(
        "trace summarized",
        extra={"model_id": model_def.algorithm_id,
               "fields": list(model_def.summary_fields)},
    )
    return summary


def node_7_audit_and_synthesis(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Fire audit hooks, persist traces, synthesize the answer (spec ``node_7``).

    Implements the spec's ``record_trace_and_respond`` step in two phases:
    (1) per selected model, write its declared ``audit_hooks`` — the
    ``trace_ledger_write`` hook becomes a ``TRACE_LEDGER_WRITE`` event
    carrying the payload hash, every other hook becomes a ``HOOK_FIRED``
    event (deployment-specific metrics wire in here); (2) synthesize
    ``final_response`` from each model's ``summary_fields``, the mean
    confidence and the audit chain tail.

    State inputs: ``reasoning_traces``, ``selected_model_ids``,
    ``domain_classification``, ``retry_count``.
    State outputs: ``current_node``, ``phase`` (``complete``),
    ``final_response``, ``all_valid=True``.

    Audit events: ``TRACE_LEDGER_WRITE``, ``HOOK_FIRED`` (per declared
    hook), ``SESSION_COMPLETED``.

    Failure semantics: reached only with fully valid traces, so no
    reasoning failure is possible here; the only escapes are
    infrastructure (a broken ledger fails closed).  Runs exactly once per
    session — the machine forbids re-entry from ``END``.

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (registry, ledger).

    Returns:
        ``(END, state)`` with ``final_response`` populated.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit).
        RegistryError: If a selected model id vanished from the registry
            (defensive; node_2/node_3 resolved the same ids earlier).

    Example:
        >>> node_7_audit_and_synthesis(state, ctx)  # doctest: +SKIP
        ('END', {..., 'escalated': False, ...})
    """
    node_id = "node_7_audit_and_synthesis"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.SYNTHESIS.value
    session_id = state["session_id"]
    log.debug(
        "node entered",
        extra={"node": node_id, "session_id": session_id,
               "models": state.get("selected_model_ids")},
    )

    traces = dict(state.get("reasoning_traces") or {})
    response_lines: list[str] = [
        "C.A.S.E. analysis complete.",
        f"Domain: {state.get('domain_classification')} — models: "
        f"{', '.join(state.get('selected_model_ids') or [])}.",
        "",
    ]
    confidences: list[float] = []

    for model_id in state.get("selected_model_ids") or []:
        record = traces.get(model_id) or {}
        model_def = ctx.registry.get(model_id)
        payload = record.get("payload") or {}
        log.debug(
            "synthesising model section",
            extra={"node": node_id, "session_id": session_id,
                   "model_id": model_id},
        )

        # 1) Trace ledger writes + audit hooks (spec node_7 step 1).
        ctx.audit(AuditEventType.TRACE_LEDGER_WRITE, session_id, node_id,
                  {"model": model_id, "payload_hash": _payload_hash(payload),
                   "attempts": record.get("attempts", 1)})
        for hook in model_def.audit_hooks:
            if hook == "trace_ledger_write":
                continue  # already written above
            ctx.audit(AuditEventType.HOOK_FIRED, session_id, node_id,
                      {"hook": hook, "model": model_id})

        # 2) Synthesis (spec node_7 step 2).
        confidence = float(payload.get("confidence") or 0.0)
        confidences.append(confidence)
        response_lines.append(
            f"[{model_id}] {model_def.algorithm_name} "
            f"(confidence {confidence:.2f}, attempts {record.get('attempts', 1)}):"
        )
        response_lines.append(f"  {_summarize_trace(model_def, record)}")
        response_lines.append("")

    if confidences:
        mean_conf = sum(confidences) / len(confidences)
        response_lines.append(
            f"Mean confidence {mean_conf:.2f}; all validation rules passed; "
            f"audit chain tail {str(ctx.last_audit_hash)[:12]}…"
        )
    if int(state.get("retry_count") or 0):
        response_lines.append(
            f"(Reflexion: {state['retry_count']} regeneration cycle(s) were required.)"
        )
    passthrough = bool(state.get("passthrough_accepted"))
    if passthrough:
        # G2: failed traces reached synthesis under passthrough_with_flags.
        # The result is flagged and all_valid is False — consumers must be
        # able to distinguish this from a fully-validated answer.
        response_lines.append(
            "(WARNING: fallback_behavior=passthrough_with_flags was applied - "
            "the traces below FAILED validation and are included unverified; "
            "see the FALLBACK_APPLIED audit event.)"
        )
        log.warning(
            "synthesis proceeding on PASSED-THROUGH unvalidated traces",
            extra={"node": node_id, "session_id": session_id},
        )

    state["final_response"] = "\n".join(response_lines)
    state["all_valid"] = not passthrough
    state["phase"] = PipelinePhase.COMPLETE.value
    # G7: persist per-session token accounting (counts only) before the
    # session-complete bracket closes.
    ctx.audit(AuditEventType.LLM_USAGE, session_id, node_id,
              _usage_event_payload(state))
    ctx.audit(AuditEventType.SESSION_COMPLETED, session_id, node_id,
              {"models": state.get("selected_model_ids") or [],
               "retry_count": state.get("retry_count", 0),
               "passthrough": passthrough})
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "session complete",
               "models": state.get("selected_model_ids") or [],
               "retry_count": state.get("retry_count", 0)},
    )
    return END_NODE, state


# --------------------------------------------------------------------------- #
# node_8 — escalate_with_context
# --------------------------------------------------------------------------- #
def node_8_human_escalation(state: dict, ctx: EngineContext) -> tuple[str, dict]:
    """Package partial traces + errors for a human reviewer (spec ``node_8``).

    Implements the spec's ``escalate_with_context`` step: the terminal,
    fail-closed outcome of the pipeline.  It packages
    ``{user_query, domain, selected_models, retry_count, validation_errors,
    error_detail, partial_traces}`` into one ``ESCALATED`` audit event and
    sets a human-facing escalation notice as ``final_response``.

    State inputs: everything accumulated so far (partial traces, feedback,
    error detail, retry count) plus the G1/G2 policy flags
    (``error_detail["reason"]``, ``abort_requested``).
    State outputs: ``current_node``, ``phase`` (``complete``),
    ``escalated=True`` (or ``aborted=True`` under the G2 abort policy),
    ``final_response``.

    Audit events: ``ESCALATED`` (the full escalation package, including the
    partial traces — evidence is preserved, never silently dropped) or
    ``SESSION_FAILED`` with reason ``aborted_per_fallback_policy`` for the
    G2 abort path.

    Failure semantics: terminal node — reached from node_1 PII policy
    refusals (G1), node_2/node_3 routing failures or node_6 budget
    exhaustion.  The session ends with whatever partial reasoning evidence
    exists at that point, packaged for the configured outcome (escalation
    or abort).

    Args:
        state: Current pipeline state (never mutated; copied on entry).
        ctx: Engine context (ledger).

    Returns:
        ``(END, state)`` with ``escalated=True`` (or ``aborted=True`` for
        the G2 abort policy) and the corresponding terminal notice in
        ``final_response``.

    Raises:
        LedgerError: Propagated from ``ctx.audit`` (fail-closed audit —
            even the escalation itself must be auditable).

    Example:
        >>> node_8_human_escalation(state, ctx)  # doctest: +SKIP
        ('END', {..., 'escalated': True, ...})
    """
    node_id = "node_8_human_escalation"
    state = _copy(state)
    state["current_node"] = node_id
    state["phase"] = PipelinePhase.ESCALATION.value
    session_id = state["session_id"]
    log.info(
        "escalation raised",
        extra={"node": node_id, "session_id": session_id,
               "retry_count": state.get("retry_count", 0),
               "validation_errors": sorted(state.get("validation_errors") or {}),
               "error_detail": state.get("error_detail")},
    )

    traces = dict(state.get("reasoning_traces") or {})
    package = {
        "user_query": state.get("user_query"),
        "domain": state.get("domain_classification"),
        "selected_models": state.get("selected_model_ids") or [],
        "retry_count": state.get("retry_count", 0),
        "validation_errors": state.get("validation_errors") or {},
        "error_detail": state.get("error_detail"),
        "partial_traces": {
            mid: {"payload": rec.get("payload"),
                  "report": rec.get("report"),
                  "attempts": rec.get("attempts", 1)}
            for mid, rec in traces.items()
        },
    }
    # G7: persist per-session token accounting (counts only) even on the
    # escalation path — cost visibility must not depend on success.
    ctx.audit(AuditEventType.LLM_USAGE, session_id, node_id,
              _usage_event_payload(state))
    ctx.audit(AuditEventType.ESCALATED, session_id, node_id, package)
    log.debug(
        "escalation package audited",
        extra={"node": node_id, "session_id": session_id,
               "package_keys": sorted(package)},
    )

    # ---- G6: deliver the escalation to the configured sink ------------- #
    # Best-effort by default: an unreachable ticketing system must not
    # turn a working escalation into a failed session.  Delivery outcome
    # is audited (HOOK_FIRED) either way, so the ledger shows whether the
    # handoff reached the outside world.
    delivered = False
    sink_error: str | None = None
    try:
        handoff = EscalationHandoff(
            session_id=session_id,
            user_query=state.get("user_query") or "",
            domain=state.get("domain_classification"),
            selected_model_ids=list(state.get("selected_model_ids") or []),
            retry_count=int(state.get("retry_count") or 0),
            validation_errors=dict(state.get("validation_errors") or {}),
            error_detail=state.get("error_detail"),
            partial_traces=package["partial_traces"],
            reason=str((state.get("error_detail") or {}).get("reason")
                       or ("aborted" if state.get("abort_requested") else "reflexion_exhausted")),
        )
        delivered = bool(ctx.escalation_sink.deliver(handoff))
    except CASEError as exc:
        sink_error = f"{type(exc).__name__}: {exc}"
        log.error(
            "escalation sink raised (strict mode?)",
            extra={"node": node_id, "session_id": session_id,
                   "sink": type(ctx.escalation_sink).__name__},
            exc_info=True,
        )
    except Exception as exc:
        sink_error = f"{type(exc).__name__}: {exc}"
        log.error(
            "escalation sink raised unexpectedly",
            extra={"node": node_id, "session_id": session_id,
                   "sink": type(ctx.escalation_sink).__name__},
            exc_info=True,
        )
    ctx.audit(AuditEventType.HOOK_FIRED, session_id, node_id,
              {"hook": "escalation_sink",
               "sink": type(ctx.escalation_sink).__name__,
               "delivered": delivered,
               **({"error": sink_error} if sink_error else {})})
    safe_incr(ctx.metrics, "case_engine_escalations_total",
              delivered="true" if delivered else "false")
    log.debug(
        "escalation sink outcome",
        extra={"node": node_id, "session_id": session_id,
               "sink": type(ctx.escalation_sink).__name__, "delivered": delivered},
    )

    # ---- Terminal outcome selection (G1 PII refusal / G2 abort) -------- #
    reason = (state.get("error_detail") or {}).get("reason")
    if state.get("abort_requested"):
        # G2 abort_session: a deliberate close, NOT a human escalation.
        state["aborted"] = True
        state["escalated"] = False
        ctx.audit(AuditEventType.SESSION_FAILED, session_id, node_id,
                  {"reason": "aborted_per_fallback_policy",
                   "policy": "abort_session",
                   "partial_traces": sorted(traces)})
        state["final_response"] = (
            "This session was aborted by engine policy.\n"
            "After exhausting the reflexion budget the configured "
            "fallback_behavior='abort_session' terminated the session "
            "instead of escalating to human review. No reasoning output "
            "is provided.\n"
            f"Reference session: {session_id}. Partial evidence is "
            "preserved in the audit ledger."
        )
        state["phase"] = PipelinePhase.COMPLETE.value
        log.info(
            "node completed",
            extra={"node": node_id, "session_id": session_id,
                   "outcome": "session aborted per fallback policy"},
        )
        return END_NODE, state

    state["escalated"] = True
    if reason == "pii_detected_without_tee":
        # G1: policy refusal — the message names categories only.
        categories = (state.get("error_detail") or {}).get("pii_categories") or []
        state["final_response"] = (
            "This request was refused by the C.A.S.E. data-protection policy.\n"
            "Personal information was detected in the input while the engine "
            "is not running inside an attested Trusted Execution Environment, "
            "and pii_policy='escalate' is configured. No reasoning was "
            "performed and no LLM generation saw the input.\n"
            f"Detected PII categories: {', '.join(categories) or 'unknown'}.\n"
            f"Reference session: {session_id}. The refusal decision is "
            "recorded in the audit ledger (TEE_DECISION event)."
        )
    else:
        state["final_response"] = (
            "This request could not be completed autonomously.\n"
            f"After {state.get('retry_count', 0)} reflexion cycle(s) the reasoning "
            "traces still failed the C.A.S.E. enforcement contracts, so the "
            "session has been escalated to a human reviewer.\n"
            f"Reference session: {session_id}. Partial traces and validation "
            "errors are preserved in the audit ledger."
        )
    state["phase"] = PipelinePhase.COMPLETE.value
    log.info(
        "node completed",
        extra={"node": node_id, "session_id": session_id,
               "outcome": "escalated to human review",
               "reason": reason},
    )
    return END_NODE, state


#: Node registry consumed by the state machine (and, via the same table,
#: by the LangGraph adapter).  The key set must equal ``EDGES``' key set —
#: a node without declared transitions is caught by
#: tests/unit/test_state_machine.py and by the topology guard at runtime.
NODES = {
    "node_1_intent_classifier": node_1_intent_classifier,
    "node_2_model_router": node_2_model_router,
    "node_3_schema_injection": node_3_schema_injection,
    "node_4_llm_generation": node_4_llm_generation,
    "node_5_ast_validator": node_5_ast_validator,
    "node_6_reflexion_loop": node_6_reflexion_loop,
    "node_7_audit_and_synthesis": node_7_audit_and_synthesis,
    "node_8_human_escalation": node_8_human_escalation,
}
