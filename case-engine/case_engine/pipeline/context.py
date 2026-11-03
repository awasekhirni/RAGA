"""EngineContext: the dependency bundle threaded through every node.

Purpose & role
--------------
Nodes are pure functions of ``(state, ctx)``; the context carries the
wired services (registry, router, gate, validator, LLM client, ledger)
plus the fail-closed audit helper.  Keeping nodes dependency-injected
makes them unit-testable without monkey-patching and makes the service
wiring of one engine instance explicit and inspectable.

Contents
--------
- :class:`EngineContext` — dataclass bundling every collaborator, the
  per-instance audit counters (``audit_count``, ``last_audit_hash``) and
  the single ``audit`` entry point every node must use to record its
  outcome.

Invariants
----------
* **Audit fail-closed** — if the ledger append fails, the exception
  propagates (after CRITICAL logging) and the pipeline halts; an
  unaudited session never continues (docs/ARCHITECTURE.md §1.3).
* One context per engine instance; it is mutated only by :meth:`audit`
  (the counters) and is otherwise treated as read-only wiring by nodes.

Cross-references: docs/PIPELINE.md (node contracts & audit events),
docs/ARCHITECTURE.md §1.3 (fail-closed everywhere),
case_engine/pipeline/nodes.py (the consumers of this context).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from case_engine.audit.base import AuditLedger
from case_engine.config import GlobalGuardrails
from case_engine.enforcement.gate import SchemaGate
from case_engine.enforcement.tool_factory import ToolFactory
from case_engine.exceptions import LedgerError
from case_engine.llm.base import BaseLLMClient
from case_engine.logging_setup import get_logger
from case_engine.observability import NoopMetrics, safe_incr
from case_engine.privacy import NoopRedactor, PIIDetector, RegexPIIDetector
from case_engine.registry.models import CognitiveRegistry
from case_engine.registry.routing import ModelRouter
from case_engine.schemas.pipeline import AuditEventType
from case_engine.validation.engine import ValidationEngine
from case_engine.validation.llm_judge import LLMJudge

__all__ = ["EngineContext"]

log = get_logger(__name__)


@dataclass
class EngineContext:
    """Shared services + counters for one engine instance.

    Built once by :class:`case_engine.engine.CASEEngine` and threaded
    through every node; nodes never construct their own dependencies, so
    tests can swap any collaborator (e.g. a mock LLM) without patching.

    Fail-closed audit contract: nodes report their outcomes exclusively
    via :meth:`audit`.  If the ledger append raises :class:`LedgerError`,
    the exception propagates and the pipeline halts rather than proceeding
    unaudited — an unaudited success is indistinguishable from a tampered
    one and is therefore never allowed.

    Attributes:
        registry: Cognitive registry — read-only source of truth mapping
            mental-model ids (``MM001``…) to typed contracts, validation
            rules and domain affinities.
        router: Cynefin router — deterministic domain -> model selection
            (affinity ranking with spec-priority tie-breaks).
        tool_factory: Provider tool-spec builder — renders a model's typed
            contract as an OpenAI-shaped tool spec.
        gate: Schema enforcement gate — re-parses and approves or rejects
            every LLM tool call before any node reads its payload.
        validator: Trace validation engine — runs each model's rule
            battery and produces a ``TraceValidationReport`` per trace.
        llm: Provider adapter — the only outbound-LLM dependency; raises
            :class:`~case_engine.exceptions.LLMAdapterError` on failure.
        ledger: Hash-chained audit ledger — the tamper-evident record of
            every externally observable step.
        guardrails: Engine guardrails (token budgets, reflexion retry cap,
            audit-every-trace flag, fallback behaviour).
        judge: Optional compliance judge (``None`` = heuristics only).
        pii_detector: PII scanner consulted by node_1 for the G1 TEE gate
            (defaults to the offline :class:`RegexPIIDetector`).
        audit_redactor: Payload redactor applied inside :meth:`audit`
            before anything reaches the ledger (G4a; default
            :class:`NoopRedactor` = byte-identical legacy behavior).
        metrics: Observability sink for counters/timers (G11; default
            :class:`~case_engine.observability.NoopMetrics` = zero
            overhead).  Instrumentation is fail-open — a raising sink
            degrades to one WARNING, never a failed session.
        escalation_sink: Delivery target for node_8 escalation handoffs
            (G6; default ``NoopEscalationSink`` = ledger-only legacy
            behaviour).  See :mod:`case_engine.escalation`.
        audit_count: Number of ledger events appended (counter).
        last_audit_hash: Tail hash of the chain (counter).

    Example:
        >>> ctx.audit(AuditEventType.DOMAIN_CLASSIFIED, sid,  # doctest: +SKIP
        ...           "node_1", {"domain": "Clear"})
        '9f2c…'      # chained hash of the appended event
    """
    registry: CognitiveRegistry
    router: ModelRouter
    tool_factory: ToolFactory
    gate: SchemaGate
    validator: ValidationEngine
    llm: BaseLLMClient
    ledger: AuditLedger
    guardrails: GlobalGuardrails
    judge: LLMJudge | None = None
    pii_detector: PIIDetector = field(default_factory=RegexPIIDetector)
    audit_redactor: Any = field(default_factory=NoopRedactor)
    metrics: Any = field(default_factory=NoopMetrics)
    escalation_sink: Any = field(default_factory=lambda: _default_sink())
    audit_count: int = field(default=0)
    last_audit_hash: str | None = field(default=None)

    def audit(
        self,
        event_type: AuditEventType,
        session_id: str,
        node: str,
        payload: dict,
    ) -> str:
        """Append one event to the ledger — **fail-closed**.

        The zero-trust posture means an unauditable session must not
        continue: a ledger failure aborts the run by raising.  This is the
        engine's most important invariant — if the append fails, the
        pipeline *halts* rather than proceeding unaudited (see
        docs/ARCHITECTURE.md §1.3, "fail-closed everywhere").

        Side effects: appends to the ledger, bumps :attr:`audit_count`,
        advances :attr:`last_audit_hash`, applies the configured audit
        redactor (G4a — PII values become stable tokens before persistence),
        and emits one INFO record plus a DEBUG record (with the payload
        shape) per appended event.  The payload itself is never logged
        verbatim above DEBUG — only its keys.

        Redaction semantics: when :attr:`audit_redactor` is a real
        redactor, the payload is redacted *before* hashing and writing, so
        the chain always commits to exactly the bytes on disk.  With the
        default :class:`NoopRedactor` the payload object is passed
        through untouched (legacy behavior, byte-identical).

        Args:
            event_type: Stable event type.
            session_id: Owning session.
            node: Emitting node id (``"engine"`` for lifecycle events).
            payload: JSON-safe event body.

        Returns:
            The chained hash of the appended event.

        Raises:
            LedgerError: Propagated after CRITICAL logging.  Callers must
                treat this as fatal for the session (fail-closed), never as
                a warning to log-and-continue.  A redactor failure also
                raises (fail-closed: an unre-readable payload must never
                be persisted raw).

        Example:
            >>> ctx.audit(AuditEventType.SESSION_START, sid, "engine", {})  # doctest: +SKIP
        """
        try:
            # G4a: redact BEFORE hashing/writing so the chain commits to
            # exactly the persisted bytes. NoopRedactor is identity.
            stored_payload, redactions = self.audit_redactor.redact_payload(payload)
            if redactions:
                log.info(
                    "audit payload redacted before persistence",
                    extra={"event_type": event_type.value,
                           "session_id": session_id, "replacements": redactions},
                )
            event = self.ledger.append(event_type.value, session_id, node, stored_payload)
        except LedgerError:
            log.critical(
                "AUDIT LEDGER UNAVAILABLE - failing closed",
                extra={"event_type": event_type.value, "session_id": session_id},
            )
            raise
        self.audit_count += 1
        self.last_audit_hash = event.hash
        # G11: every ledger event is also a metric (fail-open; labels are
        # bounded-vocabulary — event type and node id, never payload text).
        safe_incr(self.metrics, "case_engine_audit_events_total",
                  event_type=event_type.value, node=node)
        log.debug(
            "audit event appended",
            extra={"seq": event.seq, "event_type": event.event_type,
                   "session_id": session_id, "node": node,
                   "payload_keys": sorted(payload)},
        )
        log.info(
            "audit",
            extra={"seq": event.seq, "event_type": event.event_type,
                   "session_id": session_id, "node": node},
        )
        return event.hash


def _default_sink() -> Any:
    """Return the default escalation sink (imported lazily at call time).

    The import is deferred to avoid a module-import cycle:
    ``case_engine.escalation`` does not import this module, but keeping
    the default factory lazy keeps :mod:`case_engine.pipeline.context`
    importable in isolation (e.g. by doc tooling).

    Returns:
        A :class:`~case_engine.escalation.NoopEscalationSink`.
    """
    from case_engine.escalation import NoopEscalationSink

    return NoopEscalationSink()
