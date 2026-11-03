"""Optional LLM-as-Judge upgrade for semantic validation rules.

Security role
-------------
Deterministic heuristics execute first for ALL rules (offline-capable).
When a judge is configured AND the rule is semantic
(``{VR001, VR002, VR005, VR012, VR014}``), the judge's verdict is ANDed
with the heuristic (the judge can only veto, never rescue — fail-closed).
The judge is **defense-in-depth, not a primary control**: disabling it
or losing its endpoint never weakens the deterministic layer below spec
strength — it only removes one extra adversarial review.

Fail-open scope (deliberate, documented)
----------------------------------------
Judge failures are **non-fatal**: the failing audit call is logged with
a WARNING and ``run_rule`` downgrades to heuristic-only, so a flaky
judge endpoint cannot stall the pipeline.  This is the only pass-through
path in the validation stack, and it is bounded by design: the
always-on deterministic layer remains fail-closed, and even a fully
compromised judge can at worst *withhold vetoes* — it can never overturn
a heuristic rejection nor approve anything the heuristics rejected.
This fail-open posture applies to the OPTIONAL judge ONLY; every
deterministic component (gate, rules, engine) is strictly fail-closed.

Hard deadline (G3)
------------------
Because the judge is the only network call inside validation, a hung
endpoint would otherwise stall the session indefinitely despite the
``schema_validation_timeout_ms`` guardrail (which is deliberately soft
for the deterministic battery).  :class:`LLMBackedJudge` therefore runs
every audit call under a **hard deadline** (``timeout_ms``, default
1000 ms): the call executes in a daemon worker thread and, when the
deadline fires, the judge *withholds its veto* (fail-open verdict with
an explicit rationale) and the deterministic result stands.  A hung
provider call leaks one daemon thread per occurrence — documented and
bounded in impact (daemon threads never block interpreter exit).

Contents
--------
- :class:`JudgeVerdict` -- strict verdict container.
- :class:`LLMJudge` -- structural protocol for judge implementations.
- :class:`LLMBackedJudge` -- provider-backed implementation that forces
  a schema-gated ``ComplianceAuditVerdict`` tool call (no free-text
  parsing anywhere).
- ``SEMANTIC_RULE_IDS`` -- rule ids eligible for judge upgrade.

Cross-references: docs/VALIDATION_RULES.md ("Using the judge"),
docs/ARCHITECTURE.md §1.2 (schema-gated tool calls everywhere).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from case_engine.llm.base import ToolCallRequest, ToolCallResult
from case_engine.logging_setup import get_logger

__all__ = ["JudgeVerdict", "LLMJudge", "LLMBackedJudge", "SEMANTIC_RULE_IDS"]

log = get_logger(__name__)

#: Rule ids eligible for judge upgrade (the spec's "semantic" rules).
SEMANTIC_RULE_IDS: frozenset[str] = frozenset({"VR001", "VR002", "VR005", "VR012", "VR014"})


def _short(text: Any, limit: int = 160) -> str:
    """Truncate *text* for inclusion in structured log extras.

    Keeps judge rationales and error strings from flooding log lines.

    Args:
        text: Any value (coerced with ``str``).
        limit: Maximum characters kept; an ellipsis marks the cut.

    Returns:
        The truncated string.
    """
    rendered = str(text)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


class _DeadlineExceeded(Exception):
    """Internal: the judge's audit call outlived its hard deadline (G3).

    Never escapes :meth:`LLMBackedJudge.audit` — it is converted into an
    explicit fail-open verdict there.  Private to this module.
    """


class JudgeVerdict(BaseModel):
    """Verdict returned by a judge for one rule application.

    Strict container (``extra="forbid"``) so a misbehaving judge cannot
    smuggle extra keys into validation state.

    Attributes:
        passed: Whether the trace satisfied the audited rule.
        rationale: Free-text explanation citing trace evidence.

    Example:
        >>> JudgeVerdict(passed=False, rationale="orient cites no observe item")
        JudgeVerdict(passed=False, rationale='orient cites no observe item')
    """

    model_config = ConfigDict(extra="forbid")
    passed: bool
    rationale: str = ""


@runtime_checkable
class LLMJudge(Protocol):
    """Structural protocol any judge implementation must satisfy.

    Implementations need only the ``enabled`` flag plus :meth:`audit`;
    ``validation.rules.run_rule`` duck-types on this protocol, so a
    judge may wrap any provider or be a deterministic stub in tests.

    Attributes:
        enabled: Master switch consulted by ``run_rule`` before calling
            :meth:`audit` (``False`` means the judge is skipped).
    """

    enabled: bool

    def audit(self, auditor_prompt: str, payload: dict[str, Any]) -> JudgeVerdict:
        """Audit *payload* against *auditor_prompt* and return a verdict.

        Args:
            auditor_prompt: The rule's adversarial auditor prompt.
            payload: JSON-serializable ``{"rule": ..., "trace": {...}}``.

        Returns:
            The judge's :class:`JudgeVerdict`.

        Raises:
            Any exception the implementation chooses.  ``run_rule``
                treats every audit failure as a non-fatal downgrade to
                heuristic-only (fail-open scope is documented in the
                module docstring and applies to the judge ONLY).
        """
        ...  # pragma: no cover


_JUDGE_SYSTEM_PROMPT = (
    "You are the strict compliance auditor inside the C.A.S.E. reasoning "
    "engine. You audit a single mental-model trace against ONE rule. Be "
    "adversarial: reject anything that does not literally satisfy the rule. "
    "You MUST answer by calling the ComplianceAuditVerdict tool."
)

_AUDIT_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "ComplianceAuditVerdict",
        "description": (
            "Record the audit verdict for one C.A.S.E. validation rule "
            "against one reasoning trace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "passed": {
                    "type": "boolean",
                    "description": "True only if the trace fully satisfies the rule.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Cite the exact trace evidence for the verdict.",
                },
                "cited_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Quoted fragments supporting the verdict.",
                },
            },
            "required": ["passed", "rationale", "cited_evidence"],
        },
    },
}


class LLMBackedJudge:
    """Judge implementation that audits via any :class:`BaseLLMClient`.

    The judge forces a ``ComplianceAuditVerdict`` tool call so its output
    is schema-constrained like every other trace (no free-text parsing) —
    the same zero-trust tool machinery used for mental-model traces is
    applied to the auditor itself.

    Fail-open scope: :meth:`audit` never converts a judge failure into a
    rejection *or* an approval; it logs a WARNING and re-raises so
    ``run_rule`` can degrade to heuristic-only.  See the module
    docstring for why this narrow pass-through is deliberate and why it
    cannot weaken the fail-closed deterministic layer.

    Hard deadline (G3): every audit call runs under ``timeout_ms``;
    overrun produces an explicit fail-open verdict (veto withheld) with
    a WARNING record instead of an unbounded stall.

    Budget guard (G13): use :meth:`for_guardrails` to derive the
    deadline from the operator's ``schema_validation_timeout_ms``
    budget (half of it, floored at 250 ms) so the judge — the only
    unbounded network call inside validation — can never outlive its
    host validation budget.  :attr:`audit_calls` exposes a cumulative
    invocation counter for observability.

    Attributes:
        enabled: Master switch; when ``False`` rules skip judge calls.
        timeout_ms: Hard per-call deadline in milliseconds (G3); ``None``
            or ``<= 0`` disables the deadline entirely.
        audit_calls: Cumulative number of :meth:`audit` invocations
            attempted (including deadline-overrun and fail-open ones).
        _client: The provider adapter (any object with
            ``generate_tool_call``; mocks work for tests).

    Args:
        llm_client: Any provider adapter (mock works for tests).
        enabled: Master switch; when ``False`` rules skip judge calls.
        timeout_ms: Hard deadline for each audit call (default 1000 ms).
            Choose a value at or below the host's
            ``schema_validation_timeout_ms`` budget (see
            :meth:`for_guardrails` for the derived default).

    Example:
        >>> from case_engine.llm import MockLLMClient
        >>> judge = LLMBackedJudge(MockLLMClient(script={
        ...     "ComplianceAuditVerdict": {"passed": True,
        ...         "rationale": "ok", "cited_evidence": ["x"]}}))
        >>> judge.audit("Check X", {"trace": {"a": 1}}).passed
        True
    """

    def __init__(
        self,
        llm_client: Any,
        enabled: bool = True,
        timeout_ms: float | None = 1000.0,
    ) -> None:
        """Store the client, the switch and the audit deadline.

        Args:
            llm_client: Provider adapter used for audit calls.
            enabled: Master switch.
            timeout_ms: Hard deadline per audit call (G3); ``None`` or a
                non-positive value disables the deadline (not
                recommended for network-backed judges).

        Side effects:
            Emits an INFO ``LLMBackedJudge initialised`` record (config
            milestone: whether audits are active is operationally
            relevant).
        """
        self._client = llm_client
        self.enabled: bool = enabled
        self.timeout_ms: float | None = timeout_ms
        self.audit_calls: int = 0
        log.info(
            "LLMBackedJudge initialised",
            extra={"enabled": enabled, "timeout_ms": timeout_ms},
        )

    @classmethod
    def for_guardrails(
        cls,
        llm_client: Any,
        guardrails: Any,
        enabled: bool = True,
    ) -> LLMBackedJudge:
        """Build a judge whose deadline is derived from the validation budget (G13).

        The judge call is the only unbounded network call inside the
        validation battery, so its deadline is capped to **half** of
        ``guardrails.schema_validation_timeout_ms`` (floored at 250 ms
        and ceiled at the budget itself for tiny budgets).  On overrun
        the existing fail-open degradation applies (veto withheld, rule
        decided heuristically) — the deterministic layer stays
        authoritative either way.

        Args:
            llm_client: Provider adapter used for audit calls.
            guardrails: A :class:`~case_engine.config.GlobalGuardrails`
                (duck-typed: only ``schema_validation_timeout_ms`` is
                read).
            enabled: Master switch passed through.

        Returns:
            A configured :class:`LLMBackedJudge` with the derived
            deadline (INFO-logged).

        Example:
            >>> from case_engine.config import GlobalGuardrails
            >>> from case_engine.llm import MockLLMClient
            >>> j = LLMBackedJudge.for_guardrails(
            ...     MockLLMClient(script={}), GlobalGuardrails())
            >>> j.timeout_ms
            1000.0
        """
        budget_ms = int(getattr(guardrails, "schema_validation_timeout_ms", 2000) or 2000)
        derived = max(250.0, float(min(budget_ms // 2, max(budget_ms, 250))))
        log.info(
            "judge deadline derived from validation budget",
            extra={"schema_validation_timeout_ms": budget_ms,
                   "judge_timeout_ms": derived},
        )
        return cls(llm_client=llm_client, enabled=enabled, timeout_ms=derived)

    def _call_with_deadline(self, request: ToolCallRequest) -> ToolCallResult:
        """Run the client call under the configured hard deadline (G3).

        With a deadline set, the call executes in a daemon worker thread;
        the caller joins for ``timeout_ms`` and treats a live worker as a
        deadline overrun.  Client exceptions propagate unchanged through
        the holder.  Without a deadline the call is direct (legacy
        behavior).

        Args:
            request: The schema-gated judge tool-call request.

        Returns:
            The tool-call result from the client.

        Raises:
            _DeadlineExceeded: The worker outlived the deadline (the
                daemon thread is left running — documented leak bounded
                to one thread per overrun).
            Exception: Whatever the client raised (transport, schema,
                provider errors) — unchanged.
        """
        if not self.timeout_ms or self.timeout_ms <= 0:
            return self._client.generate_tool_call(request)
        holder: dict[str, Any] = {}

        def _work() -> None:
            """Worker body: capture the client result or error into *holder*.

            Runs in a daemon thread; BaseException is captured (not just
            Exception) so even a KeyboardInterrupt inside the SDK call is
            surfaced deterministically by the joiner.
            """
            try:
                holder["result"] = self._client.generate_tool_call(request)
            except BaseException as exc:
                holder["error"] = exc

        worker = threading.Thread(
            target=_work, daemon=True, name="case-judge-audit"
        )
        worker.start()
        worker.join(self.timeout_ms / 1000.0)
        if worker.is_alive():
            log.warning(
                "Judge audit worker outlived deadline",
                extra={"timeout_ms": self.timeout_ms},
            )
            raise _DeadlineExceeded(
                f"Judge audit exceeded its {self.timeout_ms}ms deadline"
            )
        if "error" in holder:
            raise holder["error"]
        return holder["result"]

    def audit(self, auditor_prompt: str, payload: dict[str, Any]) -> JudgeVerdict:
        """Run one adversarial audit turn against the configured client.

        The trace payload is embedded in the user prompt as JSON and the
        auditor must answer through the ``ComplianceAuditVerdict`` tool;
        the returned arguments are re-validated against a strict local
        contract (``extra="forbid"``) before being surfaced as a
        :class:`JudgeVerdict` — the auditor is held to the same zero-
        trust standard as the traces it audits.

        G3 deadline semantics: a client call that outlives ``timeout_ms``
        produces a **fail-open verdict** (``passed=True`` — the judge
        withholds its veto) with an explicit rationale and a WARNING
        record; the deterministic heuristic verdict below is unaffected
        and the session never stalls.

        Args:
            auditor_prompt: The rule's ``auditor_prompt`` text.
            payload: ``{"rule": ..., "trace": {...}}`` JSON-serializable.

        Returns:
            Parsed :class:`JudgeVerdict` — or the explicit fail-open
            verdict when the deadline fired.

        Raises:
            LLMAdapterError / LLMResponseError: Propagated unchanged from
                the client when the audit turn fails (transport, missing
                tool call, provider outage).  Logged at WARNING here —
                this is the documented judge fail-open degradation point
                — and then treated as a non-fatal downgrade by callers
                (``validation.rules.run_rule``).
            pydantic.ValidationError: If the auditor's tool-call
                arguments violate the verdict contract (extra keys,
                wrong types); surfaced the same way (WARNING then
                re-raise → heuristic-only downgrade).

        Logging:
            DEBUG entry (rule id), WARNING on deadline overrun or client
            failure, DEBUG for the parsed verdict.
        """
        rule_id = payload.get("rule") if isinstance(payload, dict) else None
        self.audit_calls += 1  # G13: cumulative budget/observability counter
        log.debug("Judge audit started", extra={"rule": rule_id,
                 "audit_call_no": self.audit_calls})
        user_prompt = (
            f"{auditor_prompt}\n\nAUDIT THIS TRACE (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )
        request = ToolCallRequest(
            system_prompt=_JUDGE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_specs=[_AUDIT_TOOL_SPEC],
            tool_choice="required",
            max_tokens=1024,
            metadata={"role": "compliance_auditor"},
        )
        try:
            result: ToolCallResult = self._call_with_deadline(request)
        except _DeadlineExceeded as exc:
            # G3: deadline overrun withholds the veto (fail-open) — the
            # deterministic verdict stands and the pipeline never stalls.
            log.warning(
                "Judge audit deadline exceeded - withholding veto "
                "(fail-open by design for the OPTIONAL judge only)",
                extra={"rule": rule_id, "timeout_ms": self.timeout_ms},
            )
            return JudgeVerdict(
                passed=True,
                rationale=f"{exc}; veto withheld (fail-open)",
            )
        except Exception as exc:
            # Judge-only fail-open scope: log-and-reraise lets run_rule
            # degrade to heuristic-only. Type/message are preserved.
            log.warning(
                "Judge audit call failed - degrading to heuristic-only verdict "
                "(fail-open by design for the OPTIONAL judge only)",
                extra={"rule": rule_id, "error": _short(str(exc))},
            )
            raise

        from pydantic import BaseModel as _BM

        class _AuditContract(_BM):  # local contract, mirrors tool spec
            """Strict container for the auditor's tool call."""
            model_config = ConfigDict(extra="forbid")
            passed: bool
            rationale: str
            cited_evidence: list[str] = Field(default_factory=list)

        verdict = _AuditContract.model_validate(result.arguments)
        log.debug(
            "Judge verdict",
            extra={"rule": rule_id, "passed": verdict.passed,
                   "rationale": _short(verdict.rationale, 120)},
        )
        return JudgeVerdict(passed=verdict.passed, rationale=verdict.rationale)
