"""ValidationEngine: schema gate + rule battery for one generated trace.

Security role
-------------
This is pipeline node_5 ("validate_against_registry") as a reusable
service — the *content* validation layer that runs alongside the
enforcement gate (:mod:`case_engine.enforcement.gate`): the gate proves
the payload is well-formed (schema), this engine decides whether the
reasoning inside it satisfies the mental-model contract (VR001–VR016).
Its verdict is authoritative: nothing downstream may consume a trace
that did not pass here.

Order of operations per trace:

1. **Short-circuit check** — if ``allow_unvalidated_passthrough`` is set
   the trace is passed with a loud warning (debugging only).
2. **Pydantic V2 parse** against the authoritative typed contract.
3. **Rule battery** — every rule in ``model.validation_rules`` in
   declaration order; a rule crash fails the trace (fail-closed).
4. **Latency accounting** — compared against the
   ``schema_validation_timeout_ms`` guardrail (WARNING only; the verdict
   stands, enforcement never silently degrades).

Invariants (fail-closed)
------------------------
- Validation infrastructure errors (e.g. an unresolvable contract for
  the model id) propagate as exceptions — they are NEVER converted into
  a "pass".  A bad trace can only ever produce a ``passed=False``
  report or an exception, never a silent ``passed=True``.
- Rules never run on a schema-invalid trace (the schema error list is
  the actionable output; garbage cannot fail rules meaningfully).
- The latency guardrail is advisory (WARNING); the validation verdict
  itself is never softened by slow performance.
- ``allow_unvalidated_passthrough=True`` is the single documented
  bypass; it exists only for debugging the engine and must remain
  ``False`` in production (it logs a WARNING on every use).

Cross-references: docs/VALIDATION_RULES.md (rule semantics),
docs/ARCHITECTURE.md §1.3 (fail-closed rationale), docs/PIPELINE.md
(node_5 placement).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from case_engine.config import GlobalGuardrails
from case_engine.logging_setup import get_logger
from case_engine.registry.models import CognitiveRegistry, MentalModelDefinition
from case_engine.schemas.mental_models import SCHEMA_BY_MODEL_ID
from case_engine.schemas.reports import TraceValidationReport
from case_engine.validation.llm_judge import LLMJudge
from case_engine.validation.rules import run_rule

__all__ = ["ValidationEngine"]

log = get_logger(__name__)


class ValidationEngine:
    """Validate traces against schemas and the rule battery.

    One instance serves a whole engine run: it binds the consistency-
    checked registry, the global guardrails and an optional LLM judge,
    and exposes a single entry point (:meth:`validate`) that turns a
    raw payload into an authoritative :class:`TraceValidationReport`.

    Attributes:
        _registry: Consistency-checked registry backing model lookups.
        _guardrails: Engine-wide rails (passthrough switch, latency
            budget, default confidence floor).
        _judge: Optional LLM-as-judge forwarded to semantic rules
            (VR001/002/005/012/014); may be ``None``.

    Example:
        >>> reg = CognitiveRegistry.load_default()
        >>> engine = ValidationEngine(reg, GlobalGuardrails())
        >>> payload = {"problem_statement": "x", "observe_raw_data": ["sensor a"],
        ...            "orient_context": "sensor a is high",
        ...            "decide_hypothesis": "h", "act_execution_plan": "do",
        ...            "confidence": 0.9}
        >>> report = engine.validate(reg.get("MM001"), payload)
        >>> report.passed
        True
    """

    def __init__(
        self,
        registry: CognitiveRegistry,
        guardrails: GlobalGuardrails,
        judge: LLMJudge | None = None,
    ) -> None:
        """Bind the validator to registry, guardrails and optional judge.

        The engine holds no per-session state; reports are returned, not
        stored, so one instance can validate traces for many sessions.

        Args:
            registry: Consistency-checked cognitive registry.
            guardrails: Engine guardrails (thresholds, timeout, passthrough).
            judge: Optional LLM-as-judge for semantic rules.

        Side effects:
            Emits a DEBUG ``ValidationEngine initialised`` record carrying
            the passthrough flag and judge presence (auditable config).
        """
        self._registry = registry
        self._guardrails = guardrails
        self._judge = judge
        log.debug(
            "ValidationEngine initialised",
            extra={"passthrough": guardrails.allow_unvalidated_passthrough,
                   "judge": judge is not None},
        )

    def validate(self, model_def: MentalModelDefinition, payload: dict[str, Any]) -> TraceValidationReport:
        """Validate *payload* for *model_def* end-to-end.

        Sequence: passthrough short-circuit (debug-only) → strict Pydantic
        parse → rule battery in the model's declared order → latency
        accounting.  A schema-invalid trace short-circuits the rule
        battery: rules never run on garbage.

        Fail-closed reminder: this method returns a rejection *report*
        for bad payloads, but it never converts an infrastructure fault
        (a missing contract, a registry bug) into a pass — such faults
        raise, stopping the pipeline rather than admitting an unvalidated
        trace.

        Args:
            model_def: Registry entry owning the trace.
            payload: Raw tool-call arguments.

        Returns:
            Full :class:`TraceValidationReport` (never raises for bad
            payloads — rejections are reports).

        Raises:
            KeyError: If *model_def*'s ``algorithm_id`` has no registered
                contract class — an engine/registry configuration bug.
                Deliberately NOT caught (fail-closed: an engine that
                cannot validate must stop, not pass); surfaces unlogged
                here but is a fatal, non-report outcome.

        Logging:
            DEBUG entry, WARNING on schema failure / rule-battery
            failure / passthrough use, INFO milestone on completion
            (see :meth:`_finish`).
        """
        started = time.perf_counter()
        model_id = model_def.algorithm_id
        log.debug(
            "Trace validation started",
            extra={"model_id": model_id, "n_rules": len(model_def.validation_rules)},
        )

        if self._guardrails.allow_unvalidated_passthrough:
            log.warning("UNVALIDATED PASSTHROUGH - trace accepted without checks",
                        extra={"model_id": model_id})
            return TraceValidationReport(
                model_id=model_id, tool_name=model_def.tool_name,
                schema_valid=True, passed=True,
            )

        contract = SCHEMA_BY_MODEL_ID[model_id]
        try:
            parsed = contract.model_validate(payload)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_url=False)
            ]
            log.warning("Schema validation failed", extra={"model_id": model_id, "errors": errors[:5]})
            return self._finish(model_id, model_def, False, errors, [], started)

        reports = []
        for rule_id in model_def.validation_rules:
            reports.append(run_rule(rule_id, parsed, model_def, self._guardrails, self._judge))

        passed = all(r.passed for r in reports)
        if not passed:
            failed = [r.rule_id for r in reports if not r.passed]
            log.warning("Rule battery failed", extra={"model_id": model_id, "failed": failed})
        return self._finish(model_id, model_def, True, [], reports, started)

    def _finish(
        self,
        model_id: str,
        model_def: MentalModelDefinition,
        schema_valid: bool,
        schema_errors: list[str],
        rule_reports: list,
        started: float,
    ) -> TraceValidationReport:
        """Assemble the report, enforce the latency guardrail, log the verdict.

        This is the single exit path of :meth:`validate` (both the
        schema-invalid and rule-battery outcomes funnel through here) so
        the INFO completion milestone and the latency guardrail are
        emitted exactly once per validation.

        Args:
            model_id: Mental model id.
            model_def: Registry entry.
            schema_valid: Schema parse outcome.
            schema_errors: Pydantic error strings (schema-invalid case).
            rule_reports: Rule results (schema-valid case).
            started: ``perf_counter`` start stamp.

        Returns:
            The completed :class:`TraceValidationReport`.

        Logging:
            WARNING when the latency budget is exceeded (the verdict
            still stands — enforcement never degrades with latency),
            INFO milestone with pass/fail counts.
        """
        latency_ms = (time.perf_counter() - started) * 1000.0
        passed = schema_valid and all(r.passed for r in rule_reports)
        budget = self._guardrails.schema_validation_timeout_ms
        if latency_ms > budget:
            log.warning(
                "Validation exceeded latency budget",
                extra={"model_id": model_id, "latency_ms": round(latency_ms, 1), "budget_ms": budget},
            )
        log.info(
            "Trace validation complete",
            extra={
                "model_id": model_id,
                "passed": passed,
                "schema_valid": schema_valid,
                "rules_run": len(rule_reports),
                "rules_failed": sum(1 for r in rule_reports if not r.passed),
            },
        )
        return TraceValidationReport(
            model_id=model_id,
            tool_name=model_def.tool_name,
            schema_valid=schema_valid,
            schema_errors=schema_errors,
            rule_reports=rule_reports,
            passed=passed,
            latency_ms=round(latency_ms, 3),
        )
