"""Exception hierarchy for the C.A.S.E. engine.

This module sits at the bottom of the engine's dependency graph: it
depends only on :mod:`case_engine.logging_setup`, and every other engine
module may import it freely.  The hierarchy encodes the engine's error
*policy* — who may catch what, and which failures are absorbable versus
fatal.

Design rules
------------
1. Every error raised intentionally by the engine derives from
   :class:`CASEError`, so callers can write ``except CASEError`` as a
   single catch-all boundary.
2. Errors carry a structured ``context`` dict (session id, node, model
   id, ...) in addition to the human-readable message.  The context is
   safe to log and to serialize into audit events, and ``__str__``
   renders it deterministically (keys sorted).
3. Errors never leak provider SDK internals: LLM SDK exceptions are
   wrapped in :class:`LLMAdapterError` at the adapter boundary.
4. Construction is side-effect-light: a single DEBUG record (message
   truncated to 200 chars, context *keys* only — never values) is
   emitted, so fail-closed paths stay observable even when the pipeline
   absorbs the exception into an escalation.

Contents
--------
- :class:`CASEError` — root of the hierarchy (structured context).
- Configuration: :class:`ConfigurationError`.
- Registry & routing: :class:`RegistryError`, :class:`RoutingError`.
- Enforcement & validation: :class:`SchemaViolationError`,
  :class:`ToolCallRejectedError`.
- LLM adapters: :class:`LLMAdapterError`, :class:`LLMResponseError`,
  :class:`LLMProviderUnavailableError`.
- Pipeline: :class:`PipelineError`, :class:`TopologyViolationError`,
  :class:`ReflexionExhaustedError`, :class:`EscalationRequiredError`.
- Audit ledger: :class:`LedgerError`, :class:`LedgerTamperError`.
- Result store (G12): :class:`StorageError`.
- Escalation delivery (G6): :class:`EscalationDeliveryError` lives in
  :mod:`case_engine.escalation` (next to the sinks it describes).

See also: docs/ARCHITECTURE.md (error strategy),
case_engine/pipeline/context.py (the fail-closed audit helper),
case_engine/engine.py (the ``except`` boundary that converts failures
into escalations).

Example
-------
>>> from case_engine.exceptions import RoutingError
>>> try:
...     raise RoutingError("Unknown domain", domain="Fortnite")
... except CASEError as exc:
...     print(exc.context["domain"])
Fortnite
"""

from __future__ import annotations

from typing import Any

from case_engine.logging_setup import get_logger

__all__ = [
    "CASEError",
    "ConfigurationError",
    "RegistryError",
    "SchemaViolationError",
    "ToolCallRejectedError",
    "RoutingError",
    "LLMAdapterError",
    "LLMResponseError",
    "LLMProviderUnavailableError",
    "PipelineError",
    "TopologyViolationError",
    "ReflexionExhaustedError",
    "EscalationRequiredError",
    "LedgerError",
    "LedgerTamperError",
    "StorageError",
]

log = get_logger(__name__)


class CASEError(Exception):
    """Base class for every error intentionally raised by the engine.

    Args:
        message: Human-readable description of the failure.  When omitted
            the subclass ``default_message`` is used.
        **context: Structured key/value metadata (e.g. ``session_id``,
            ``node``, ``model_id``) attached to the exception and included
            in log records.

    Attributes:
        context: Dict of structured metadata describing the failure.
        default_message: Class-level fallback message.
    """

    default_message: str = "An unspecified C.A.S.E. engine error occurred."

    def __init__(self, message: str | None = None, **context: Any) -> None:
        """Initialise the error with an optional message and structured context.

        Side effects: stores ``context`` unchanged and emits one DEBUG
        record (error type, message truncated to 200 chars, context
        *keys* only) so fail-closed paths remain observable when the
        pipeline absorbs the exception into an escalation.  Context
        values are deliberately never logged here — they may carry
        session payloads.

        Args:
            message: Human-readable description of the failure.  When
                omitted or empty the subclass ``default_message`` is used.
            **context: Structured key/value metadata (e.g. ``session_id``,
                ``node``, ``model_id``) attached to the exception, rendered
                by :meth:`__str__` and safe to log or audit.

        Example:
            >>> err = CASEError("boom", session_id="s1", node="node_4")
            >>> err.context["node"]
            'node_4'
        """
        self.context: dict[str, Any] = context
        super().__init__(message or self.default_message)
        log.debug(
            "case error constructed",
            extra={"error_type": type(self).__name__,
                   "error_message": str(message or self.default_message)[:200],
                   "context_keys": sorted(context)},
        )

    def __str__(self) -> str:
        """Render the message with the structured context appended.

        The identifying metadata (session, node, model id, ...) is shown
        in brackets so tracebacks carry it without callers having to
        inspect ``exc.context``.

        Returns:
            ``"<message> [k=v, ...]"`` with context keys sorted for
            deterministic output; just ``"<message>"`` when no context
            was supplied.  Never raises.

        Example:
            >>> str(RoutingError("Unknown domain", domain="Fortnite"))
            "Unknown domain [domain='Fortnite']"
        """
        base = super().__str__()
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
            return f"{base} [{ctx}]"
        return base


class ConfigurationError(CASEError):
    """Raised when engine configuration is missing, malformed or contradictory.

    Raised by: :meth:`case_engine.config.EngineConfig.from_file` (missing
    file / invalid JSON), the ledger factory for unknown backends
    (``case_engine/audit/__init__.py``) and the provider factory for
    unknown providers (``case_engine/llm/__init__.py``).  Logged at
    ERROR at the wrapping boundary before the raise.
    """

    default_message = "Invalid C.A.S.E. engine configuration."


class RegistryError(CASEError):
    """Raised when a mental-model / validation-rule lookup fails or the registry is inconsistent.

    Raised by: ``case_engine/registry/models.py`` (unknown
    algorithm/rule/tool ids, duplicate ids, routing-matrix
    inconsistencies), ``case_engine/enforcement/tool_factory.py`` (spec
    building for an unknown model) and ``case_engine/validation/
    rules.py`` (unimplemented rule).  The missing identifier rides in
    ``context`` (e.g. ``algorithm_id``).
    """

    default_message = "Cognitive registry lookup or consistency failure."


class RegistryPackError(RegistryError):
    """Raised when an external registry pack cannot be loaded or validated.

    Raised by: ``case_engine/registry/pack.py`` (G15) — unreadable
    pack files, invalid JSON, a wrong ``schema_version``, entries that
    fail the frozen Pydantic models, or duplicate ids.  The pack path
    and the offending entry id (when known) ride in ``context``.
    """

    default_message = "External registry pack is missing, malformed or inconsistent."


class SchemaViolationError(CASEError):
    """Raised when a payload does not satisfy a mental-model Pydantic contract.

    Rarely raised directly — the enforcement gate raises its subclass
    :class:`ToolCallRejectedError`.  Also the documented failure mode of
    LLM-judge verdict parsing (``case_engine/validation/llm_judge.py``).
    """

    default_message = "Payload violates the mental-model schema contract."


class ToolCallRejectedError(SchemaViolationError):
    """Raised by the enforcement gate when an LLM tool call is rejected.

    This is the exception behind the "fill-in-the-blank" forcing function:
    no user-facing response can be produced until a tool call passes
    Pydantic validation.  The pipeline absorbs it in the reflexion loop
    (regenerate with explicit validation feedback) rather than letting it
    propagate; failing field paths are carried in ``context`` and logged
    as a WARNING at the gate.
    """

    default_message = "Tool call rejected by the C.A.S.E. enforcement gate."


class RoutingError(CASEError):
    """Raised when the Cynefin domain cannot be routed to mental models.

    Raised by: ``case_engine/registry/routing.py`` (unknown domain, or an
    empty selection after affinity/threshold filtering).  The pipeline
    absorbs it — an unroutable session escalates to a human instead of
    crashing the run.
    """

    default_message = "Unable to route the classified domain to cognitive models."


class LLMAdapterError(CASEError):
    """Base class for LLM provider adapter failures (transport, auth, SDK missing).

    Wrapping boundary: provider SDK exceptions never escape as
    third-party types — adapters convert them into the subclasses below
    (provider name in ``context``) after logging at ERROR with the
    original traceback attached.
    """

    default_message = "LLM adapter failure."


class LLMResponseError(LLMAdapterError):
    """Raised when the provider response does not contain a usable tool call.

    Typical triggers: empty choice, plain text instead of a tool call,
    or a tool call whose arguments fail JSON parsing.  Absorbed by the
    reflexion loop; once the retry budget is exhausted the session
    escalates.
    """

    default_message = "LLM response did not contain a valid tool call."


class LLMProviderUnavailableError(LLMAdapterError):
    """Raised when an optional provider SDK is not installed or endpoint unreachable.

    Raised at first use by the provider clients
    (``case_engine/llm/*_client.py``, lazy SDK import) and by the
    LangGraph parity adapter.  This is an *infrastructure* failure: with
    ``fallback="raise"`` it propagates out of
    :meth:`case_engine.engine.CASEEngine.run`.
    """

    default_message = "LLM provider SDK is not installed or endpoint is unreachable."


class PipelineError(CASEError):
    """Raised when the execution pipeline itself fails (not a validation rejection).

    Signals an engine bug or invariant breach rather than a bad LLM
    answer.  :meth:`case_engine.engine.CASEEngine.run` catches this
    hierarchy and either packages a human escalation (default) or
    re-raises, per the ``fallback_behavior`` guardrail.
    """

    default_message = "Execution pipeline failure."


class TopologyViolationError(PipelineError):
    """Raised when a node attempts an illegal transition (graph topology guard).

    Raised by: ``case_engine/pipeline/state_machine.py`` when a node
    returns a next-node id outside its declared successor set — always
    an engine bug, never user input.
    """

    default_message = "Illegal state-machine transition requested."


class ReflexionExhaustedError(PipelineError):
    """Raised when the reflexion loop consumed all retries without a valid trace.

    Reserved for pipeline/adapter integrations that need the retry-budget
    breach as an exception; the built-in nodes instead mark the session
    for escalation so partial traces survive.
    """

    default_message = "Reflexion retries exhausted without a compliant reasoning trace."


class EscalationRequiredError(PipelineError):
    """Raised when the engine must hand the session to a human reviewer.

    Reserved signalling variant of :class:`PipelineError`; the stock
    engine surfaces escalation through the ``CASEResult.escalated`` flag
    instead, keeping :meth:`case_engine.engine.CASEEngine.run`
    non-throwing for reasoning failures.
    """

    default_message = "Session escalated to human review."


class LedgerError(CASEError):
    """Raised when the audit ledger cannot append or be read.

    Raised by: ``case_engine/audit/jsonl_ledger.py`` and
    ``case_engine/audit/sqlite_ledger.py`` (open / replay / append
    failures), always with ``from exc`` chaining after ERROR logging.
    Because auditing is fail-closed, this aborts the run.
    """

    default_message = "Audit ledger failure."


class LedgerTamperError(LedgerError):
    """Raised when hash-chain verification detects tampering.

    Reserved for hard-fail verification integrations; the stock ledgers
    and :meth:`case_engine.engine.CASEEngine.verify_audit_chain` report
    tampering through the returned :class:`ChainVerification` instead of
    raising.
    """

    default_message = "Audit ledger hash-chain verification FAILED - records were modified."


class StorageError(CASEError):
    """Raised when the optional session-result store cannot operate (G12).

    Raised by: ``case_engine/storage.py`` (open/query failures).  Unlike
    the audit ledger the result store is *best-effort*: save failures are
    logged and swallowed by the engine (the caller still receives the
    result), so this exception surfaces mainly at construction/query
    time.  Logged at ERROR before raising with ``from exc`` chaining.
    """

    default_message = "Session result store failure."
