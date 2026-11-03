"""Human-in-the-loop escalation: structured handoffs + pluggable sinks (G6).

Node_8 packages partial traces into an ``ESCALATED`` audit event — but a
ticket nobody receives is a dead end.  This module closes the loop in
two directions:

* **Outbound**: :class:`EscalationSink` implementations deliver a typed
  :class:`EscalationHandoff` to the outside world (JSONL file for
  ticketing pipelines, structured log for ops, HTTP webhook for
  PagerDuty/Slack-class integrations).  Delivery is **best-effort by
  default**: an unreachable ticketing system must never turn a working
  escalation into a failed session — failures are logged at ERROR and,
  when a sink runs in ``strict=True`` mode, raised as
  :class:`EscalationDeliveryError` for callers that want the opposite
  contract.
* **Inbound**: :meth:`CASEEngine.resume` (see ``engine.py``) feeds a
  human verdict back into the engine as a *guided re-run*: a fresh
  deterministic session seeded with the original problem plus the
  reviewer's verdict as operator context data.  The verdict never
  becomes instructions to the LLM — it is data the models reason about,
  and the full pipeline (gates, rules, audit) applies to the resumed
  session exactly as to any other.

Contents
--------
- :class:`EscalationHandoff` — the typed ticket object.
- :class:`EscalationSink` — structural sink protocol.
- :class:`NoopEscalationSink` — default sink (delivers nowhere, logs DEBUG).
- :class:`LoggingEscalationSink` — WARNING-log delivery.
- :class:`FileEscalationSink` — append-only JSONL delivery (fsync'd).
- :class:`WebhookEscalationSink` — stdlib-HTTP POST delivery
  (``urllib.request``, no third-party dependency).
- :func:`handoff_from_result` — build a handoff from a
  :class:`~case_engine.schemas.pipeline.CASEResult`.

Cross-references: ``docs/GAP_ANALYSIS.md`` G6 (origin),
``case_engine/pipeline/nodes.py::node_8_human_escalation`` (delivery
point), ``case_engine/engine.py::CASEEngine.resume`` (the return path),
``docs/PIPELINE.md`` (node_8 contract).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from case_engine.exceptions import CASEError
from case_engine.logging_setup import get_logger

__all__ = [
    "EscalationDeliveryError", "EscalationHandoff", "EscalationSink",
    "NoopEscalationSink", "LoggingEscalationSink", "FileEscalationSink",
    "WebhookEscalationSink", "handoff_from_result",
]

log = get_logger(__name__)


class EscalationDeliveryError(CASEError):
    """Raised when a *strict* escalation sink cannot deliver a handoff.

    Only raised in ``strict=True`` mode (and by the engine's delivery
    helper when every configured sink failed *and* strict delivery was
    requested); the default best-effort contract logs at ERROR and lets
    the session's escalation result stand — a broken ticketing system is
    an ops problem, not a reason to lose the evidence package.
    """

    default_message = "Escalation handoff could not be delivered."


class EscalationHandoff(BaseModel):
    """Typed, self-contained ticket object produced by node_8 (G6).

    Everything a reviewer needs to make a decision lives here: the
    original problem, how far the deterministic pipeline got, which
    validation contracts failed and which partial traces exist.  The
    object is JSON-safe by construction (Pydantic) and is exactly what
    :meth:`CASEEngine.resume` consumes to start the human-in-the-loop
    return path.

    Attributes:
        session_id: The escalated session.
        user_query: Original raw problem statement.
        domain: Classified (or preset) domain, when known.
        selected_model_ids: Models that were routed (may have executed).
        retry_count: Reflexion cycles consumed before escalation.
        validation_errors: Remaining feedback per model id.
        error_detail: Structured reason (PII refusal, abort, routing …).
        partial_traces: ``model_id -> {payload, report, attempts}`` —
            the evidence package (payloads already ledger-redacted).
        reason: Short machine-readable escalation reason.
        created_at: ISO-8601 UTC creation stamp.
        metadata: Sink/deployment-specific free-form extras (JSON-safe).

    Example:
        >>> h = EscalationHandoff(session_id="s1", user_query="q",
        ...                       reason="reflexion_exhausted")
        >>> h.session_id
        's1'
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    user_query: str = ""
    domain: str | None = None
    selected_model_ids: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    validation_errors: dict[str, str] = Field(default_factory=dict)
    error_detail: dict[str, Any] | None = None
    partial_traces: dict[str, Any] = Field(default_factory=dict)
    reason: str = "escalated"
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO-8601 UTC creation stamp (auto-filled)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EscalationSink(Protocol):
    """Structural contract for escalation delivery targets (G6).

    Implementations must be thread-safe (node_8 may run in any thread)
    and must document their strictness: best-effort sinks log failures
    and return; strict sinks raise :class:`EscalationDeliveryError` so
    the engine can surface the outage.
    """

    def deliver(self, handoff: EscalationHandoff) -> bool:
        """Deliver one handoff.

        Args:
            handoff: The typed ticket.

        Returns:
            ``True`` when the handoff reached its target; ``False`` when
            delivery failed under the best-effort contract (strict sinks
            raise instead of returning ``False``).
        """
        ...  # pragma: no cover - protocol


class NoopEscalationSink:
    """Default sink — records nothing, logs one DEBUG line.

    Keeps the engine's legacy behaviour (escalations live in the audit
    ledger only) while making the delivery point explicit and testable.

    Example:
        >>> s = NoopEscalationSink()
        >>> s.deliver(EscalationHandoff(session_id="s1"))
        False
    """

    def deliver(self, handoff: EscalationHandoff) -> bool:
        """Log and discard the handoff (never raises).

        Args:
            handoff: Ignored beyond the DEBUG metadata line.

        Returns:
            ``False`` — nothing was delivered anywhere.
        """
        log.debug(
            "escalation handoff dropped (NoopEscalationSink)",
            extra={"session_id": handoff.session_id, "reason": handoff.reason},
        )
        return False


class LoggingEscalationSink:
    """Deliver escalations as structured WARNING log records.

    Useful for deployments where the ops channel *is* the log pipeline
    (journald, CloudWatch, Loki).  Payload contents (query, traces,
    errors) are never logged — only ids, counts and the reason, per the
    engine's log-safety rules.

    Example:
        >>> s = LoggingEscalationSink()
        >>> s.deliver(EscalationHandoff(session_id="s1", reason="x"))
        True
    """

    def deliver(self, handoff: EscalationHandoff) -> bool:
        """Emit one structured WARNING describing the handoff.

        Args:
            handoff: The ticket to announce.

        Returns:
            ``True`` (logging cannot fail; callers treat this as
            delivered).
        """
        log.warning(
            "ESCALATION HANDOFF - human review required",
            extra={"session_id": handoff.session_id,
                   "reason": handoff.reason,
                   "domain": handoff.domain,
                   "selected_models": handoff.selected_model_ids,
                   "retry_count": handoff.retry_count,
                   "partial_trace_models": sorted(handoff.partial_traces),
                   "error_detail_keys": sorted(handoff.error_detail or {})},
        )
        return True


class FileEscalationSink:
    """Append-only JSONL delivery (one :class:`EscalationHandoff` per line).

    The file is the integration contract for ticketing pipelines: JSON
    lines, fsync'd per delivery, thread-safe via an in-process lock.
    Lines carry full handoff payloads — route the file through the same
    access controls as the audit ledger (both may embed partial traces).

    Args:
        path: Target JSONL file (created with parents on first delivery).
        strict: When ``True``, delivery failures raise
            :class:`EscalationDeliveryError` instead of returning
            ``False``.

    Raises:
        EscalationDeliveryError: Raised at *delivery* time (not
            construction) when the file cannot be written and
            ``strict=True``; always logged at ERROR first.

    Example:
        >>> import tempfile, os
        >>> path = os.path.join(tempfile.mkdtemp(), "escalations.jsonl")
        >>> s = FileEscalationSink(path)
        >>> s.deliver(EscalationHandoff(session_id="s1"))
        True
    """

    def __init__(self, path: str | Path, strict: bool = False) -> None:
        """Store the target path (no I/O happens until first delivery).

        Args:
            path: Target JSONL file path.
            strict: Fail-closed delivery contract switch.
        """
        self._path = Path(path)
        self._strict = strict
        self._lock = threading.Lock()
        log.info("FileEscalationSink configured",
                 extra={"path": str(self._path), "strict": strict})

    def deliver(self, handoff: EscalationHandoff) -> bool:
        """Append the handoff as one JSON line (fsync'd).

        Args:
            handoff: The ticket to persist.

        Returns:
            ``True`` on success; ``False`` on I/O failure in best-effort
            mode (logged at ERROR with the traceback first).

        Raises:
            EscalationDeliveryError: On I/O failure when ``strict=True``
                (chained from the underlying ``OSError``).
        """
        line = handoff.model_dump_json()
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError as exc:
            log.error(
                "escalation handoff delivery failed",
                extra={"sink": "file", "path": str(self._path),
                       "session_id": handoff.session_id},
                exc_info=True,
            )
            if self._strict:
                raise EscalationDeliveryError(
                    f"FileEscalationSink could not write {self._path}: {exc}"
                ) from exc
            return False
        log.info(
            "escalation handoff delivered",
            extra={"sink": "file", "path": str(self._path),
                   "session_id": handoff.session_id, "reason": handoff.reason},
        )
        return True


class WebhookEscalationSink:
    """HTTP POST delivery using the standard library (no extra dependency).

    Posts the handoff as a JSON document to *url* with a 5-second
    default timeout.  Any non-2xx response, network error or payload
    encoding failure counts as a failed delivery: logged at ERROR, and
    either returned as ``False`` (best-effort) or raised as
    :class:`EscalationDeliveryError` (strict).  The webhook URL is
    logged in full (it is configuration, not a secret); any
    ``Authorization`` header *value* is never logged.

    Args:
        url: Absolute ``http(s)`` endpoint receiving the JSON POST.
        timeout_s: Per-delivery timeout in seconds.
        headers: Extra request headers (e.g. auth tokens — values are
            never logged).
        strict: Raise instead of returning ``False`` on failure.

    Example:
        >>> s = WebhookEscalationSink("https://ops.example.com/hooks/case")
        >>> s.timeout_s
        5.0
    """

    def __init__(
        self,
        url: str,
        timeout_s: float = 5.0,
        headers: dict[str, str] | None = None,
        strict: bool = False,
    ) -> None:
        """Validate and store the webhook configuration (no I/O yet).

        Args:
            url: Absolute ``http(s)`` endpoint.
            timeout_s: Per-delivery timeout (must be > 0).
            headers: Extra headers; ``Content-Type`` is set automatically.
            strict: Fail-closed delivery contract switch.

        Raises:
            ValueError: When *url* is not an ``http(s)`` URL or
                ``timeout_s <= 0`` — logged at ERROR first.
        """
        if not (url.startswith("http://") or url.startswith("https://")):
            log.error("webhook sink rejected non-http url",
                      extra={"scheme": url.split(":", 1)[0][:10]})
            raise ValueError("WebhookEscalationSink url must be http(s)")
        if timeout_s <= 0:
            log.error("webhook sink rejected non-positive timeout",
                      extra={"timeout_s": timeout_s})
            raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
        self._url = url
        self._timeout_s = float(timeout_s)
        self._headers = {"Content-Type": "application/json",
                         **(headers or {})}
        self._strict = strict
        log.info("WebhookEscalationSink configured",
                 extra={"url": url, "timeout_s": self._timeout_s,
                        "header_names": sorted(self._headers), "strict": strict})

    @property
    def timeout_s(self) -> float:
        """Return the configured per-delivery timeout in seconds."""
        return self._timeout_s

    def deliver(self, handoff: EscalationHandoff) -> bool:
        """POST the handoff JSON to the configured endpoint.

        Args:
            handoff: The ticket to deliver.

        Returns:
            ``True`` on a 2xx response; ``False`` on any failure in
            best-effort mode (logged at ERROR first).

        Raises:
            EscalationDeliveryError: On any delivery failure when
                ``strict=True`` (chained from the underlying error).
        """
        body = json.dumps(handoff.model_dump(), ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, headers=self._headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                status = int(getattr(response, "status", 0) or 0)
                if not 200 <= status < 300:
                    # URLError (not HTTPError): we only have a status code,
                    # no response object, and URLError keeps the typing
                    # contract of urllib honest.
                    raise urllib.error.URLError(
                        f"non-2xx escalation delivery response (status {status})"
                    )
        except Exception as exc:
            log.error(
                "escalation webhook delivery failed",
                extra={"sink": "webhook", "url": self._url,
                       "session_id": handoff.session_id,
                       "error_type": type(exc).__name__},
                exc_info=True,
            )
            if self._strict:
                raise EscalationDeliveryError(
                    f"WebhookEscalationSink POST to {self._url} failed: {exc}"
                ) from exc
            return False
        log.info(
            "escalation handoff delivered",
            extra={"sink": "webhook", "url": self._url,
                   "session_id": handoff.session_id, "reason": handoff.reason},
        )
        return True


def handoff_from_result(
    result: Any,
    *,
    package: dict[str, Any] | None = None,
    reason: str = "escalated",
    metadata: dict[str, Any] | None = None,
) -> EscalationHandoff:
    """Build an :class:`EscalationHandoff` from a ``CASEResult``.

    Convenience bridge for integrations that hold a result (e.g. from
    :meth:`CASEEngine.run`) and want a ticket without touching pipeline
    internals.  *package* (the node_8 ``ESCALATED`` payload) is optional
    richer evidence; when omitted the handoff is built from the result
    alone.

    Args:
        result: A :class:`~case_engine.schemas.pipeline.CASEResult`
            (duck-typed to avoid a circular import).
        package: Optional node_8 escalation package
            (``partial_traces``, ``error_detail``, ``domain``).
        reason: Machine-readable escalation reason.
        metadata: Deployment-specific extras carried verbatim.

    Returns:
        The typed handoff, ready for any :class:`EscalationSink`.

    Example:
        >>> class _R:  # minimal duck-typed result
        ...     session_id = "s9"; user_query = "q"; escalated = True
        ...     domain_classification = "Complex"; retry_count = 3
        ...     selected_model_ids = ["MM001"]; errors = {"MM001": "VR001: x"}
        >>> h = handoff_from_result(_R(), reason="reflexion_exhausted")
        >>> h.session_id, h.retry_count
        ('s9', 3)
    """
    package = package or {}
    traces = package.get("partial_traces")
    if not isinstance(traces, dict):
        traces = {
            t.model_id: {"payload": t.payload, "report": None,
                         "attempts": t.attempts}
            for t in (getattr(result, "traces", None) or [])
        }
    handoff = EscalationHandoff(
        session_id=result.session_id,
        user_query=getattr(result, "user_query", ""),
        domain=package.get("domain") or getattr(result, "domain_classification", None),
        selected_model_ids=list(getattr(result, "selected_model_ids", None) or []),
        retry_count=int(getattr(result, "retry_count", 0) or 0),
        validation_errors=dict(getattr(result, "errors", None) or {}),
        error_detail=package.get("error_detail"),
        partial_traces=traces,
        reason=reason,
        metadata=dict(metadata or {}),
    )
    log.debug(
        "escalation handoff built from result",
        extra={"session_id": handoff.session_id, "reason": reason,
               "trace_models": sorted(handoff.partial_traces)},
    )
    return handoff
