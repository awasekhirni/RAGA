"""CASEEngine — the top-level orchestrating facade.

This module is the *composition root* of the C.A.S.E. engine: the only
place where every subsystem (registry, router, enforcement gate,
validation engine, LLM adapter, audit ledger) is constructed and wired
into the shared :class:`~case_engine.pipeline.context.EngineContext`
consumed by the pipeline nodes.  Nothing downstream imports this module,
so it may depend on all of them.

One object wires everything together and exposes three capabilities:

* :meth:`CASEEngine.run` — execute the full deterministic pipeline.
* :meth:`CASEEngine.verify_audit_chain` — tamper-evidence sweep.
* Introspection: :meth:`list_models`, :meth:`describe_model`,
  :meth:`tool_spec`.

Invariants / guarantees
-----------------------
- Fail-safe contract: :meth:`run` never raises for *reasoning* failures
  (those become escalations); it only raises :class:`CASEError` for
  infrastructure failures the operator must see (bad config, unavailable
  ledger when fallback is ``raise``).
- The constructor never touches the network — collaborators are built,
  never contacted; the first provider call happens inside :meth:`run`.
- Every session is bracketed by ``SESSION_START`` and
  ``SESSION_COMPLETED``/``SESSION_FAILED`` audit events, so the ledger
  contains no orphaned sessions.
- Wiring is deterministic: identical constructor arguments produce an
  identically wired engine.
- Logging discipline: query previews are truncated (120 chars), full
  prompt payloads are never logged above DEBUG, and provider secrets
  (``llm_kwargs`` values) are never logged at all.

See also: docs/ARCHITECTURE.md (system design), docs/PIPELINE.md (node
semantics), case_engine/pipeline/state_machine.py (the executor invoked
by :meth:`CASEEngine.run`), case_engine/pipeline/nodes.py (the 8
topology nodes).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from case_engine.audit import create_ledger
from case_engine.audit.base import AUDIT_EVENT_SCHEMA_VERSION, ChainVerification
from case_engine.audit.crypto import AEADPayloadCipher
from case_engine.config import EngineConfig
from case_engine.enforcement import SchemaGate, ToolFactory
from case_engine.exceptions import (
    CASEError,
    ConfigurationError,
)
from case_engine.llm import create_llm_client
from case_engine.logging_setup import configure_logging, get_logger
from case_engine.messages import msg
from case_engine.observability import NoopMetrics, safe_incr
from case_engine.pipeline.context import EngineContext
from case_engine.pipeline.nodes import END_NODE, ENTRY_NODE
from case_engine.pipeline.state_machine import CASEStateMachine
from case_engine.privacy import (
    NoopRedactor,
    PIIDetector,
    PIIRedactor,
    RegexPIIDetector,
)
from case_engine.registry import CognitiveRegistry, ModelRouter
from case_engine.schemas.pipeline import AuditEventType, CASEResult, CASEState, TokenUsage
from case_engine.schemas.reports import TraceRecord
from case_engine.storage import create_result_store
from case_engine.validation import ValidationEngine
from case_engine.validation.llm_judge import LLMJudge
from case_engine.version import ENGINE_NAME, __version__

__all__ = ["CASEEngine"]

log = get_logger(__name__)


class CASEEngine:
    """Deterministic mental-model reasoning engine (the meta-agent constitution).

    The engine guarantees, by construction:

    1. **Every** LLM interaction is a schema-gated tool call (Strategy 1).
    2. Control flow follows the declared 8-node topology — the LLM has
       zero agency over it (Strategy 2).
    3. Failed generations are reflexion-retried with explicit validation
       feedback (Strategy 3) and ultimately escalate to a human.
    4. Every step is hash-chained into the audit ledger.

    Example:
        Silence engine logging first (constructor and node milestones
        emit INFO records that would otherwise pollute doctest output)
        and use an in-memory ledger so the example leaves no files:

        >>> from case_engine.logging_setup import configure_logging
        >>> from case_engine.config import EngineConfig
        >>> _ = configure_logging(level="CRITICAL")
        >>> from case_engine import CASEEngine
        >>> engine = CASEEngine(config=EngineConfig(ledger_backend="memory"))
        >>> result = engine.run("Zone 4 sensors spike anomalously")
        >>> result.all_valid
        True
        >>> engine.verify_audit_chain().valid
        True

    Args:
        config: :class:`EngineConfig` (defaults resolved from env/file).
        llm_client: Injected adapter; overrides ``config.provider``.
        ledger: Injected ledger; overrides ``config.ledger_backend``.
        judge: Optional :class:`~case_engine.validation.llm_judge.LLMJudge`.
        registry: Injected registry (tests/custom packs).
        pii_detector: Injected PII scanner for the node_1 TEE gate (G1);
            defaults to the offline :class:`RegexPIIDetector`.
        audit_redactor: Injected audit-payload redactor (G4a); defaults
            to :class:`PIIRedactor` when ``config.redact_pii_in_audit``
            is set, else the identity :class:`NoopRedactor`.

    Attributes:
        config: The resolved :class:`EngineConfig` in force for this instance.
        guardrails: Shorthand for ``config.guardrails`` (read by nodes).
        registry: The loaded cognitive registry (models + rules).
        llm_client: The provider adapter behind every generation step.
        ledger: The hash-chained audit ledger backing ``ctx.audit``.
        judge: Optional LLM-backed compliance judge (``None`` = heuristics).
        pii_detector: The PII scanner consulted by node_1 (G1 TEE gate).
        audit_redactor: The redactor applied to every audit payload before
            persistence (G4a); identity when redaction is disabled.
        metrics: The observability sink behind every counter/timer (G11;
            default :class:`NoopMetrics` = zero overhead).
        escalation_sink: The node_8 delivery target (G6; default
            ``NoopEscalationSink`` = ledger-only legacy behaviour).
        result_store: The optional session-result store (G12; ``None``
            when ``result_store_backend="none"``).
        ctx: The shared :class:`~case_engine.pipeline.context.EngineContext`
            handed to every node; also carries the live ``audit_count``
            and ``last_audit_hash`` counters.
        ENTRY_NODE: Convenience re-export of the topology entry node id.
        END_NODE: Convenience re-export of the topology end node id.
    """

    def __init__(
        self,
        config: EngineConfig | None = None,
        llm_client: Any | None = None,
        ledger: Any | None = None,
        judge: LLMJudge | None = None,
        registry: CognitiveRegistry | None = None,
        pii_detector: PIIDetector | None = None,
        audit_redactor: Any | None = None,
        metrics: Any | None = None,
        escalation_sink: Any | None = None,
        result_store: Any | None = None,
    ) -> None:
        """Resolve and wire all collaborators (constructor never touches the network).

        Resolution order per collaborator: an explicit injection argument
        wins over the config-derived default, so tests and embedders can
        substitute mocks without touching configuration.  Construction is
        offline by design — adapters and ledgers are instantiated, never
        contacted; the first provider call happens inside :meth:`run`.

        Side effects: loads the registry from its packaged data, may read
        ``CASE_ENGINE_CONFIG`` (via :meth:`EngineConfig.load` when
        *config* is omitted), lazily installs engine logging handlers
        (only when no handler exists on the ``case_engine`` root logger),
        and emits one INFO milestone plus DEBUG wiring records.  Provider
        credentials in ``config.llm_kwargs`` are never logged — only
        kwarg names.

        Args:
            config: :class:`EngineConfig` (defaults resolved from env/file).
            llm_client: Injected adapter; overrides ``config.provider``.
            ledger: Injected ledger; overrides ``config.ledger_backend``.
            judge: Optional :class:`~case_engine.validation.llm_judge.LLMJudge`.
                Use :meth:`LLMBackedJudge.for_guardrails` (G13) to derive
                its deadline from ``schema_validation_timeout_ms``.
            registry: Injected registry (tests/custom packs).
            pii_detector: Injected PII scanner for the node_1 TEE gate (G1);
                defaults to the offline :class:`RegexPIIDetector`.
            audit_redactor: Injected audit-payload redactor (G4a); defaults
                to :class:`PIIRedactor` when ``config.redact_pii_in_audit``
                is set, else the identity :class:`NoopRedactor`.
            metrics: Injected observability sink (G11); defaults to
                :class:`~case_engine.observability.NoopMetrics` unless
                ``config.log_level="DEBUG"`` (then :class:`InMemoryMetrics`
                is installed for debugging convenience — a DEBUG run can
                always answer "how many gate rejections?" without extra
                wiring).
            escalation_sink: Injected node_8 delivery target (G6); when
                omitted, built from ``config.escalation_sink_kind``.
            result_store: Injected session-result store (G12); when
                omitted, built from ``config.result_store_backend``.

        Raises:
            ConfigurationError: Unknown ledger backend or provider key,
                or an unparsable config file picked up via the
                ``CASE_ENGINE_CONFIG`` environment variable (logged).
            RegistryError: The packaged cognitive registry is internally
                inconsistent (duplicate ids, dangling rule references).
            LLMProviderUnavailableError: A non-mock provider SDK is not
                installed (propagated from ``create_llm_client``).
        """
        self.config = config or EngineConfig.load()
        self.guardrails = self.config.guardrails
        self.registry = registry or CognitiveRegistry.load_default()
        self.llm_client = llm_client or create_llm_client(
            self.config.provider, **self.config.llm_kwargs
        )
        self.pii_detector = pii_detector or RegexPIIDetector()
        self.audit_redactor = audit_redactor or (
            PIIRedactor() if self.config.redact_pii_in_audit else NoopRedactor()
        )
        payload_cipher = None
        if self.config.ledger_encryption:
            if ledger is not None:
                log.warning(
                    "ledger_encryption=True is ignored for an injected ledger "
                    "- configure the cipher on the ledger itself",
                )
            else:
                try:
                    payload_cipher = AEADPayloadCipher()
                except CASEError as exc:
                    log.error(
                        "ledger encryption requested but unavailable - failing fast",
                        extra={"error": str(exc)},
                    )
                    raise ConfigurationError(
                        f"ledger_encryption=True but the cipher/key is unavailable: {exc}"
                    ) from exc
                if self.config.ledger_backend == "memory":
                    log.warning(
                        "ledger_encryption=True has no effect on the memory backend "
                        "(volatile; nothing persists to encrypt)",
                    )
        self.ledger = ledger or create_ledger(
            self.config.ledger_backend, self.config.ledger_path,
            payload_cipher=payload_cipher,
        )
        self.judge = judge

        # G11: observability sink — explicit injection wins; DEBUG runs get
        # a free InMemoryMetrics so debug output can answer "how many?".
        if metrics is not None:
            self.metrics = metrics
        elif self.config.log_level.upper() == "DEBUG":
            from case_engine.observability import InMemoryMetrics

            self.metrics = InMemoryMetrics()
            log.info("DEBUG log level - InMemoryMetrics installed")
        else:
            self.metrics = NoopMetrics()

        # G6: escalation delivery — injection wins; else config-derived.
        if escalation_sink is not None:
            self.escalation_sink = escalation_sink
        else:
            from case_engine.escalation import (
                FileEscalationSink,
                LoggingEscalationSink,
                NoopEscalationSink,
            )

            kind = self.config.escalation_sink_kind
            if kind == "file":
                self.escalation_sink = FileEscalationSink(
                    self.config.escalation_sink_path or "case_escalations.jsonl")
            elif kind == "logging":
                self.escalation_sink = LoggingEscalationSink()
            else:
                self.escalation_sink = NoopEscalationSink()

        # G12: session-result store — injection wins; else config-derived.
        if result_store is not None:
            self.result_store = result_store
        else:
            self.result_store = create_result_store(
                self.config.result_store_backend, self.config.result_store_path,
            )

        log.debug(
            "engine collaborators resolved",
            extra={"provider": self.llm_client.name,
                   "ledger": type(self.ledger).__name__,
                   "registry_size": len(self.registry),
                   "llm_kwarg_keys": sorted(self.config.llm_kwargs),
                   "judge": type(self.judge).__name__ if self.judge else None,
                   "pii_detector": type(self.pii_detector).__name__,
                   "audit_redactor": type(self.audit_redactor).__name__,
                   "metrics": type(self.metrics).__name__,
                   "escalation_sink": type(self.escalation_sink).__name__,
                   "result_store": type(self.result_store).__name__
                   if self.result_store else None},
        )

        # Library-courteous logging: only configure when nothing is set up.
        if not logging.getLogger("case_engine").handlers:
            configure_logging(self.config.log_level, json_mode=self.config.log_json)

        router = ModelRouter(self.registry)
        tool_factory = ToolFactory()
        gate = SchemaGate(self.registry)
        validator = ValidationEngine(self.registry, self.guardrails, judge=self.judge)
        self.ctx = EngineContext(
            registry=self.registry,
            router=router,
            tool_factory=tool_factory,
            gate=gate,
            validator=validator,
            llm=self.llm_client,
            ledger=self.ledger,
            guardrails=self.guardrails,
            judge=self.judge,
            pii_detector=self.pii_detector,
            audit_redactor=self.audit_redactor,
            metrics=self.metrics,
            escalation_sink=self.escalation_sink,
        )
        log.info(
            "CASEEngine initialised",
            extra={"version": __version__, "provider": self.llm_client.name,
                   "ledger": type(self.ledger).__name__, "models": len(self.registry),
                   "pii_gate": self.guardrails.tee_required_for_pii,
                   "pii_policy": self.guardrails.pii_policy,
                   "audit_redaction": self.config.redact_pii_in_audit,
                   "ledger_encrypted": self.config.ledger_encryption,
                   "metrics": type(self.metrics).__name__,
                   "escalation_sink": type(self.escalation_sink).__name__,
                   "result_store": (type(self.result_store).__name__
                                    if self.result_store else "none")},
        )

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run(
        self,
        user_query: str,
        context: dict[str, Any] | None = None,
        domain_hint: str | None = None,
        forced_model_ids: list[str] | None = None,
        tee_attested: bool | None = None,
    ) -> CASEResult:
        """Run the full pipeline for one user query.

        Synchronous, deterministic entry point: seeds a fresh
        :class:`~case_engine.schemas.pipeline.CASEState` (new session id,
        UTC timestamp), appends a ``SESSION_START`` audit event, then
        hands control to
        :class:`~case_engine.pipeline.state_machine.CASEStateMachine`.
        Reasoning failures (invalid traces, exhausted reflexion) are
        absorbed by the pipeline and surface as a flagged escalation in
        the returned result — only infrastructure failures propagate.

        G1 data protection: node_1 scans the input for PII; when PII is
        found without TEE attestation, the session follows
        ``guardrails.pii_policy`` (fail-closed escalation by default).
        Attestation resolves from the *tee_attested* argument when given,
        else from ``config.tee_attested``.

        Side effects: appends audit events via ``self.ctx.audit`` and
        emits INFO/DEBUG logs (query previews truncated to 120 chars;
        full prompts never logged above DEBUG).

        Args:
            user_query: Non-empty problem statement (must be a ``str``).
            context: Optional JSON-safe context injected into prompts.
            domain_hint: Optional Cynefin/specialist domain preset
                (skips the LLM classifier, audited as ``DOMAIN_PRESET``).
            forced_model_ids: Optional operator override of the router.
            tee_attested: Per-call TEE attestation override (G1);
                ``None`` defers to ``config.tee_attested``.

        Returns:
            :class:`CASEResult` — success, a flagged escalation, a policy
            refusal (``pii_flagged=True`` + ``tee_decision="escalated"``),
            an abort (``aborted=True``) or a flagged passthrough
            (``passthrough=True``) per configuration.

        Raises:
            CASEError: Only for infrastructure failures (bad query,
                topology bug, ledger failure with ``fallback="raise"``);
                every raise is preceded by an ERROR log with context.
        """
        if not isinstance(user_query, str) or not user_query.strip():
            log.error(
                "run rejected: user_query must be a non-empty string",
                extra={"query_type": type(user_query).__name__},
            )
            raise CASEError("user_query must be a non-empty string")

        session_id = str(uuid.uuid4())
        attested = bool(self.config.tee_attested if tee_attested is None else tee_attested)
        log.debug(
            "pipeline run starting",
            extra={"session_id": session_id, "query_preview": user_query[:120],
                   "domain_hint": domain_hint, "forced_model_ids": forced_model_ids,
                   "has_context": bool(context),
                   "tee_attested": attested,
                   "pii_policy": self.guardrails.pii_policy,
                   "fallback_behavior": self.guardrails.fallback_behavior},
        )
        started_at = datetime.now(UTC).isoformat()
        ctx_count_before = self.ctx.audit_count

        self.ctx.audit(
            AuditEventType.SESSION_START,
            session_id, "engine",
            {"engine_version": __version__, "engine_name": ENGINE_NAME,
             "provider": self.llm_client.name,
             "audit_event_schema_version": AUDIT_EVENT_SCHEMA_VERSION,
             "query_preview": user_query[:120]},
        )

        state: CASEState = {
            "session_id": session_id,
            "user_query": user_query,
            "context": context or {},
            "domain_classification": domain_hint,
            "domain_source": None,
            "selected_model_ids": [],
            "forced_model_ids": forced_model_ids,
            "reasoning_traces": {},
            "validation_errors": {},
            "retry_count": 0,
            "all_valid": False,
            "final_response": None,
            "current_node": ENTRY_NODE,
            "next_node": None,
            "phase": None,
            "tool_specs": [],
            "error_detail": None,
            "escalated": False,
            "started_at": started_at,
            "finished_at": None,
            "pii_detected": False,
            "pii_categories": [],
            "tee_attested": attested,
            "tee_decision": None,
            "abort_requested": False,
            "passthrough_accepted": False,
            "llm_usage": {"calls": 0, "prompt_tokens": 0,
                          "completion_tokens": 0, "total_tokens": 0},
        }

        try:
            final_state = CASEStateMachine(self.ctx).run(dict(state))
        except CASEError as exc:
            # Engine bug, enforcement fault or ledger failure — never
            # silently degraded.  G23: *every* failure that propagates
            # (including ToolCallRejectedError enforcement faults) closes
            # the session with a terminal SESSION_FAILED audit event so
            # no run can vanish between the brackets unaudited.
            log.exception(
                "Pipeline failure (infrastructure or enforcement fault)",
                extra={"session_id": session_id,
                       "error_type": type(exc).__name__,
                       "fallback_behavior": self.guardrails.fallback_behavior},
            )
            self._audit_session_failed(session_id, exc)
            if self.guardrails.fallback_behavior == "escalate_to_human_with_partial_trace":
                log.warning(
                    "infrastructure failure packaged as human escalation",
                    extra={"session_id": session_id,
                           "error_type": type(exc).__name__},
                )
                escalation = self._escalation_result(
                    state, session_id, exc, ctx_count_before)
                self._store_result(escalation)
                return escalation
            log.error(
                "re-raising infrastructure failure (fallback_behavior='raise')",
                extra={"session_id": session_id,
                       "error_type": type(exc).__name__},
            )
            raise

        result = self._result_from_state(final_state, ctx_count_before)
        self._store_result(result)
        log.info(
            "session complete",
            extra={"session_id": result.session_id, "all_valid": result.all_valid,
                   "escalated": result.escalated, "retry_count": result.retry_count,
                   "audit_events": result.audit_event_count,
                   "llm_calls": result.token_usage.calls,
                   "total_tokens": result.token_usage.total_tokens},
        )
        return result

    async def run_async(
        self,
        user_query: str,
        context: dict[str, Any] | None = None,
        domain_hint: str | None = None,
        forced_model_ids: list[str] | None = None,
        tee_attested: bool | None = None,
    ) -> CASEResult:
        """Async twin of :meth:`run` — non-blocking event loop (G5, phase 1).

        Identical validation, audit bracketing, state seeding, result
        shaping, escalation packaging and result-store persistence as
        the synchronous path; the pipeline executes via
        :meth:`CASEStateMachine.run_async`, whose nodes run in worker
        threads so the awaiting event loop is never blocked by provider
        round-trips.  Semantics, determinism and error behaviour are
        exactly :meth:`run`'s — read that docstring first; only the
        concurrency mechanics differ.

        Args:
            user_query: Non-empty problem statement (must be a ``str``).
            context: Optional JSON-safe context injected into prompts.
            domain_hint: Optional Cynefin/specialist domain preset.
            forced_model_ids: Optional operator override of the router.
            tee_attested: Per-call TEE attestation override (G1).

        Returns:
            :class:`CASEResult` — the same outcome vocabulary as
            :meth:`run` (success, escalation, refusal, abort,
            passthrough).

        Raises:
            CASEError: Only for infrastructure failures (identical
                semantics to :meth:`run`; every raise preceded by an
                ERROR log).

        Example:
            >>> import asyncio
            >>> from case_engine import CASEEngine
            >>> from case_engine.config import EngineConfig
            >>> engine = CASEEngine(config=EngineConfig(ledger_backend="memory"))
            >>> result = asyncio.run(engine.run_async("Zone 4 sensors spike"))  # doctest: +SKIP
            >>> result.all_valid                       # doctest: +SKIP
            True
        """
        if not isinstance(user_query, str) or not user_query.strip():
            log.error(
                "run_async rejected: user_query must be a non-empty string",
                extra={"query_type": type(user_query).__name__},
            )
            raise CASEError("user_query must be a non-empty string")

        session_id = str(uuid.uuid4())
        attested = bool(self.config.tee_attested if tee_attested is None else tee_attested)
        log.debug(
            "pipeline run starting (async)",
            extra={"session_id": session_id, "query_preview": user_query[:120],
                   "domain_hint": domain_hint, "forced_model_ids": forced_model_ids,
                   "has_context": bool(context), "tee_attested": attested,
                   "pii_policy": self.guardrails.pii_policy,
                   "fallback_behavior": self.guardrails.fallback_behavior},
        )
        started_at = datetime.now(UTC).isoformat()
        ctx_count_before = self.ctx.audit_count

        self.ctx.audit(
            AuditEventType.SESSION_START,
            session_id, "engine",
            {"engine_version": __version__, "engine_name": ENGINE_NAME,
             "provider": self.llm_client.name,
             "audit_event_schema_version": AUDIT_EVENT_SCHEMA_VERSION,
             "query_preview": user_query[:120]},
        )

        state: CASEState = {
            "session_id": session_id,
            "user_query": user_query,
            "context": context or {},
            "domain_classification": domain_hint,
            "domain_source": None,
            "selected_model_ids": [],
            "forced_model_ids": forced_model_ids,
            "reasoning_traces": {},
            "validation_errors": {},
            "retry_count": 0,
            "all_valid": False,
            "final_response": None,
            "current_node": ENTRY_NODE,
            "next_node": None,
            "phase": None,
            "tool_specs": [],
            "error_detail": None,
            "escalated": False,
            "started_at": started_at,
            "finished_at": None,
            "pii_detected": False,
            "pii_categories": [],
            "tee_attested": attested,
            "tee_decision": None,
            "abort_requested": False,
            "passthrough_accepted": False,
            "llm_usage": {"calls": 0, "prompt_tokens": 0,
                          "completion_tokens": 0, "total_tokens": 0},
        }

        try:
            final_state = await CASEStateMachine(self.ctx).run_async(dict(state))
        except CASEError as exc:
            # Async twin of the sync failure bracket (G23): audit every
            # propagating failure, including enforcement faults.
            log.exception(
                "Pipeline failure (infrastructure or enforcement fault, async)",
                extra={"session_id": session_id,
                       "error_type": type(exc).__name__,
                       "fallback_behavior": self.guardrails.fallback_behavior},
            )
            self._audit_session_failed(session_id, exc)
            if self.guardrails.fallback_behavior == "escalate_to_human_with_partial_trace":
                log.warning(
                    "infrastructure failure packaged as human escalation (async)",
                    extra={"session_id": session_id,
                           "error_type": type(exc).__name__},
                )
                escalation = self._escalation_result(
                    state, session_id, exc, ctx_count_before)
                self._store_result(escalation)
                return escalation
            log.error(
                "re-raising infrastructure failure (fallback_behavior='raise', async)",
                extra={"session_id": session_id,
                       "error_type": type(exc).__name__},
            )
            raise

        result = self._result_from_state(final_state, ctx_count_before)
        self._store_result(result)
        log.info(
            "session complete (async)",
            extra={"session_id": result.session_id, "all_valid": result.all_valid,
                   "escalated": result.escalated, "retry_count": result.retry_count,
                   "audit_events": result.audit_event_count,
                   "llm_calls": result.token_usage.calls,
                   "total_tokens": result.token_usage.total_tokens},
        )
        return result

    # ------------------------------------------------------------------ #
    # Introspection & audit
    # ------------------------------------------------------------------ #
    def verify_audit_chain(self) -> ChainVerification:
        """Verify the full hash chain of the audit ledger.

        Delegates to the injected ledger's ``verify_chain`` sweep: every
        record's payload hash and the prev-hash linkage are recomputed,
        so any retro-active edit, deletion or reordering is detected.

        Side effects: emits a DEBUG record with the verification
        outcome; performs no writes and never raises for a tampered
        ledger (tampering is a *result*, not an exception).

        Returns:
            :class:`ChainVerification` with ``valid=True`` when pristine;
            otherwise ``valid=False`` plus ``reason`` and
            ``first_invalid_seq`` diagnostics.
        """
        verification = self.ledger.verify_chain()
        log.debug(
            "audit chain verified",
            extra={"valid": verification.valid,
                   "total_entries": verification.total_entries,
                   "first_invalid_seq": verification.first_invalid_seq},
        )
        return verification

    def list_models(self) -> list[dict[str, Any]]:
        """Return a compact summary of every registered mental model.

        Operator-facing projection of the registry: one flat dict per
        model, directly JSON-serialisable.  Ordering follows the
        registry (stable insertion order).

        Side effects: emits a DEBUG record with the row count.

        Returns:
            List of dicts: id, name, category, threshold, rules, affinity.
        """
        models = [
            {
                "algorithm_id": m.algorithm_id,
                "name": m.algorithm_name,
                "category": m.category,
                "confidence_threshold": m.confidence_threshold,
                "validation_rules": list(m.validation_rules),
                "domain_affinity": dict(m.domain_affinity),
            }
            for m in self.registry.all_models()
        ]
        log.debug("listed registry models", extra={"count": len(models)})
        return models

    def describe_model(self, algorithm_id: str) -> dict[str, Any]:
        """Return the full registry entry for *algorithm_id*.

        Args:
            algorithm_id: Registry key, upper-cased by convention,
                e.g. ``"MM005"``.

        Returns:
            The model dump (all registry fields) as a JSON-safe dict.

        Side effects: emits a DEBUG record identifying the model.

        Raises:
            RegistryError: Unknown id (propagated from the registry;
                logged there before the raise).
        """
        entry = self.registry.get(algorithm_id)
        log.debug("described model", extra={"algorithm_id": algorithm_id,
                                            "name": entry.algorithm_name})
        return entry.model_dump()

    def tool_spec(self, algorithm_id: str) -> dict[str, Any]:
        """Return the OpenAI-shaped tool spec for *algorithm_id*.

        The spec is what gets offered to the LLM as the *only* callable
        tool for this model (Strategy 1: schema-gated tool calls); it is
        also the contract external agent stacks must honour.

        Args:
            algorithm_id: Registry key, e.g. ``"MM005"``.

        Returns:
            The provider tool spec (for wiring external agent stacks).

        Side effects: emits a DEBUG record identifying the model.

        Raises:
            RegistryError: Unknown id (propagated from the registry).
        """
        spec = self.ctx.tool_factory.openai_tool(self.registry.get(algorithm_id))
        log.debug("built tool spec", extra={"algorithm_id": algorithm_id})
        return spec

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _audit_session_failed(self, session_id: str, exc: Exception) -> None:
        """Write a SESSION_FAILED event (best-effort; never masks the original).

        Called from the infrastructure-failure path of :meth:`run` so the
        ledger still records why the session ended.  A second failure
        here (ledger unavailable *during* failure accounting) is logged
        at CRITICAL with the traceback and swallowed — the original
        exception must keep propagating untouched.

        Args:
            session_id: Owning session.
            exc: The infrastructure failure being accounted for.
        """
        try:
            self.ctx.audit(AuditEventType.SESSION_FAILED, session_id, "engine",
                           {"error": str(exc), "type": type(exc).__name__})
        except CASEError:  # pragma: no cover - ledger double failure
            log.critical("Ledger unavailable during failure accounting",
                         extra={"session_id": session_id}, exc_info=True)

    def _escalation_result(
        self,
        state: CASEState | dict[str, Any],
        session_id: str,
        exc: Exception,
        ctx_count_before: int,
    ) -> CASEResult:
        """Build an escalation result after an infrastructure failure.

        Packages the pre-run state into a human-reviewable
        :class:`CASEResult`: ``escalated=True``, a stable escalation
        message embedding the failure type and the reference session id,
        and partial audit accounting preserved.

        Side effects: emits a DEBUG record with the failure type; does
        not append audit events (that is :meth:`_audit_session_failed`'s
        job).

        Args:
            state: The pre-run state.
            session_id: Owning session.
            exc: The failure.
            ctx_count_before: Audit count before the run.

        Returns:
            A flagged :class:`CASEResult` (never raises for the wrapped
            failure — the message is derived from ``exc`` defensively).
        """
        log.debug(
            "packaging escalation result",
            extra={"session_id": session_id, "error_type": type(exc).__name__},
        )
        return CASEResult(
            session_id=session_id,
            user_query=state.get("user_query", ""),
            escalated=True,
            all_valid=False,
            retry_count=state.get("retry_count", 0),
            # G19: catalog-seam message — with no catalog installed this
            # renders the same English text as before; a catalog template
            # keyed "escalation.infra_failure" may restate it localized.
            final_response=msg(
                "escalation.infra_failure",
                default=(
                    "The C.A.S.E. engine hit an internal failure and escalated "
                    f"the session to human review ({type(exc).__name__}: {exc}). "
                    f"Reference session: {session_id}."
                ),
                error_type=type(exc).__name__,
                error=str(exc),
                session_id=session_id,
            ),
            errors={"engine": str(exc)},
            started_at=state.get("started_at", ""),
            finished_at=datetime.now(UTC).isoformat(),
            audit_event_count=self.ctx.audit_count - ctx_count_before,
            last_audit_hash=self.ctx.last_audit_hash,
            pii_flagged=bool(state.get("pii_detected")),
            pii_categories=list(state.get("pii_categories") or []),
            tee_decision=state.get("tee_decision"),
            aborted=False,
            passthrough=False,
        )

    def _result_from_state(
        self, state: CASEState | dict[str, Any], ctx_count_before: int,
    ) -> CASEResult:
        """Serialize final pipeline state into a :class:`CASEResult`.

        Pure projection: validates each collected reasoning trace into a
        :class:`~case_engine.schemas.reports.TraceRecord`, counts the
        audit events appended during the session and snapshots the chain
        tail hash.  ``finished_at`` is stamped here (UTC, ISO-8601);
        ``started_at`` is carried over from the run state.

        Side effects: emits a DEBUG record with the trace count.

        Args:
            state: Final state from the state machine.
            ctx_count_before: Audit count before the run.

        Returns:
            The user-facing result.

        Raises:
            pydantic.ValidationError: If a stored trace does not satisfy
                the ``TraceRecord`` contract (an engine bug, not a
                user-facing failure path).
        """
        traces = [
            TraceRecord.model_validate(record)
            for record in (state.get("reasoning_traces") or {}).values()
        ]
        usage_agg = state.get("llm_usage") or {}
        token_usage = TokenUsage(
            calls=int(usage_agg.get("calls", 0) or 0),
            prompt_tokens=int(usage_agg.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_agg.get("completion_tokens", 0) or 0),
        )
        token_usage.total_tokens = (token_usage.prompt_tokens
                                    + token_usage.completion_tokens)
        log.debug(
            "serialising pipeline state to result",
            extra={"session_id": state.get("session_id"), "trace_count": len(traces),
                   "llm_calls": token_usage.calls,
                   "total_tokens": token_usage.total_tokens},
        )
        return CASEResult(
            session_id=state["session_id"],
            user_query=state["user_query"],
            domain_classification=state.get("domain_classification"),
            domain_source=state.get("domain_source"),
            selected_model_ids=list(state.get("selected_model_ids") or []),
            traces=traces,
            all_valid=bool(state.get("all_valid")),
            final_response=state.get("final_response"),
            escalated=bool(state.get("escalated")),
            retry_count=int(state.get("retry_count") or 0),
            errors=dict(state.get("validation_errors") or {}),
            started_at=state.get("started_at", ""),
            finished_at=datetime.now(UTC).isoformat(),
            audit_event_count=self.ctx.audit_count - ctx_count_before,
            last_audit_hash=self.ctx.last_audit_hash,
            pii_flagged=bool(state.get("pii_detected")),
            pii_categories=list(state.get("pii_categories") or []),
            tee_decision=state.get("tee_decision"),
            aborted=bool(state.get("abort_requested")),
            passthrough=bool(state.get("passthrough_accepted")),
            token_usage=token_usage,
        )

    def _store_result(self, result: CASEResult) -> None:
        """Persist a completed result into the optional store (G12).

        Best-effort by contract: the result store is an operational
        index, not the tamper-evident record (that is the ledger), so a
        save failure logs at ERROR and never fails the session — the
        caller always receives the result object.

        Args:
            result: The completed session result.
        """
        if self.result_store is None:
            return
        try:
            saved = self.result_store.save(result)
        except Exception:
            log.error(
                "result store raised on save (ignored)",
                extra={"session_id": result.session_id,
                       "store": type(self.result_store).__name__},
                exc_info=True,
            )
            return
        if not saved:
            log.warning(
                "result store reported a failed save",
                extra={"session_id": result.session_id,
                       "store": type(self.result_store).__name__},
            )
        safe_incr(self.metrics, "case_engine_results_stored_total",
                  stored="true" if saved else "false")

    # ------------------------------------------------------------------ #
    # Human-in-the-loop round trip (G6)
    # ------------------------------------------------------------------ #
    def resume(
        self,
        prior: Any,
        reviewer_verdict: str,
        reviewer_id: str = "human-reviewer",
        approve: bool = True,
        context: dict[str, Any] | None = None,
        tee_attested: bool | None = None,
    ) -> CASEResult:
        """Re-run a session after a human verdict on an escalation (G6).

        Closes the node_8 round trip: the escalation package (partial
        traces, validation errors, the original problem) is fed back as
        a **guided re-run** — a fresh deterministic session whose
        operator context carries the reviewer's verdict as *data*:

        ``context = {"review": {"prior_session_id", "reviewer_id",
        "approved", "verdict"}, "prior_partial_traces": {...}}``

        The verdict never becomes instructions to the LLM; the full
        constitution (PII gate, routing, schema gate, rule battery,
        reflexion budget, hash-chained audit) applies to the resumed
        session exactly as to any other, and the new session is
        bracketed by its own ``SESSION_START``/``SESSION_COMPLETED``
        events with ``resume_from`` recorded in the start payload.

        Args:
            prior: The escalated session evidence — a
                :class:`~case_engine.schemas.pipeline.CASEResult` (as
                returned by :meth:`run`) **or** an
                :class:`~case_engine.escalation.EscalationHandoff`
                (e.g. loaded from a :class:`FileEscalationSink` file).
            reviewer_verdict: The human decision text (kept verbatim as
                context data; truncated previews only in logs).
            reviewer_id: Reviewer identifier for the audit trail.
            approve: ``True`` (default) when the reviewer authorises a
                regeneration attempt; ``False`` records a rejection —
                the re-run still executes but the verdict data marks it,
                and operators typically route rejections elsewhere.
            context: Optional extra operator context merged under the
                resumed session (never overriding the ``review`` key).
            tee_attested: Per-call TEE attestation override (G1),
                forwarded to the underlying run.

        Returns:
            A fresh :class:`CASEResult` for the resumed session (stored
            in the result store when one is configured).

        Raises:
            CASEError: Propagated from :meth:`run` under the same
                infrastructure-failure semantics (bad input, ledger
                failure with ``fallback_behavior='raise'``).

        Example:
            >>> prior = engine.run("Zone 4 sensors spike")   # doctest: +SKIP
            >>> verdict = engine.resume(prior, "Approved - proceed")  # doctest: +SKIP
            >>> verdict.session_id != prior.session_id        # doctest: +SKIP
            True
        """
        prior_session, prior_query, prior_traces = self._prior_evidence(prior)
        merged_context: dict[str, Any] = dict(context or {})
        review_block = {
            "prior_session_id": prior_session,
            "reviewer_id": reviewer_id,
            "approved": bool(approve),
            "verdict": reviewer_verdict,
        }
        merged_review = dict(merged_context.get("review") or {})
        merged_review.update(review_block)
        merged_context["review"] = merged_review
        if prior_traces:
            merged_context["prior_partial_traces"] = prior_traces
        log.info(
            "resuming escalated session with human verdict",
            extra={"prior_session_id": prior_session,
                   "reviewer_id": reviewer_id,
                   "approved": bool(approve),
                   "verdict_preview": reviewer_verdict[:120]},
        )
        safe_incr(self.metrics, "case_engine_resumes_total",
                  approved="true" if approve else "false")
        self.ctx.audit(
            AuditEventType.HOOK_FIRED,
            prior_session, "engine",
            {"hook": "resume", "reviewer_id": reviewer_id,
             "approved": bool(approve)},
        )
        return self.run(prior_query, context=merged_context,
                        tee_attested=tee_attested)

    @staticmethod
    def _prior_evidence(prior: Any) -> tuple[str, str, dict[str, Any]]:
        """Extract ``(session_id, user_query, partial_traces)`` from *prior*.

        Accepts a :class:`CASEResult` or an :class:`EscalationHandoff`
        (duck-typed); unknown shapes fail fast with a logged error.

        Args:
            prior: The escalation evidence object.

        Returns:
            ``(prior_session_id, user_query, partial_traces)``.

        Raises:
            CASEError: When *prior* lacks the required evidence fields
                (logged at ERROR first).
        """
        session_id = getattr(prior, "session_id", None)
        query = getattr(prior, "user_query", None)
        if not session_id or query is None:
            log.error("resume called with unusable prior evidence",
                      extra={"prior_type": type(prior).__name__})
            raise CASEError(
                "resume() needs a CASEResult or EscalationHandoff with "
                "session_id and user_query",
            )
        traces = getattr(prior, "partial_traces", None)
        if not isinstance(traces, dict):
            # CASEResult: reconstruct a compact evidence view from records.
            traces = {
                t.model_id: {"payload": t.payload, "report": None,
                             "attempts": t.attempts}
                for t in (getattr(prior, "traces", None) or [])
            }
        return str(session_id), str(query), traces

    # Convenience re-export for callers building custom entry flows.
    ENTRY_NODE = ENTRY_NODE
    END_NODE = END_NODE
