"""In-memory audit ledger (tests, demos, CI).  Not durable by design.

Implements the identical hash-chain contract as the persistent backends
(:class:`~case_engine.audit.jsonl_ledger.JSONLLedger`,
:class:`~case_engine.audit.sqlite_ledger.SQLiteLedger`) but keeps every
event in a process-local list: when the process exits, the trail is
gone.  That is a *feature* — it makes tests fast and hermetic — but it
must never be selected for deployments that need evidence.

Invariants
----------
* Same chain math, same genesis anchor, same fail-closed verification
  semantics as every other backend; a chain built here verifies
  byte-identically if re-imported into a persistent backend.
* Thread-safe within the process (a lock serializes appends); there is
  intentionally no cross-process story.
* No update/delete API — append-only by construction.

Contents
--------
- :class:`InMemoryLedger` — volatile ledger for tests, CI and demos.

Cross-references: ``docs/AUDIT_LEDGER.md`` §2 (backend selection table)
and ``tests/unit/test_ledgers.py`` (chain-math tests run against this
backend).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from case_engine.audit.base import AuditEvent, AuditLedger, compute_event_hash
from case_engine.logging_setup import get_logger

__all__ = ["InMemoryLedger"]

log = get_logger(__name__)


class InMemoryLedger(AuditLedger):
    """Volatile ledger implementing the identical chain contract.

    Concurrency: thread-safe within the process (append lock).
    Persistence: none — the event list dies with the process, which is
    exactly why tests and CI should prefer it and deployments must not.

    Example:
        >>> ledger = InMemoryLedger()
        >>> ledger.append("SESSION_START", "s-1", "engine", {}).seq
        1
        >>> ledger.verify_chain().valid
        True
    """

    def __init__(self) -> None:
        """Start an empty chain.

        Logged at DEBUG (non-durable ledger creation is routine in
        tests and must not pollute INFO streams).
        """
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()
        log.debug("InMemoryLedger ready (non-durable)")

    def append(
        self,
        event_type: str,
        session_id: str,
        node: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """Append one chained event in memory.

        The chain state (``_seq`` / ``_last_hash``) advances under the
        lock immediately after the event object is built — there is no
        I/O that can fail, so this backend cannot raise a
        :class:`~case_engine.exceptions.LedgerError`.

        Args:
            event_type: Stable event-type string.
            session_id: Owning session.
            node: Emitting node.
            payload: JSON-safe body (never logged; only its field count).

        Returns:
            The chained :class:`AuditEvent`.
        """
        log.debug(
            "ledger append requested",
            extra={"event_type": event_type, "session_id": session_id,
                   "node": node, "payload_fields": len(payload)},
        )
        with self._lock:
            seq = self._seq + 1
            timestamp = datetime.now(UTC).isoformat()
            event_hash = compute_event_hash(
                self._last_hash, seq, timestamp, event_type, session_id, node, payload
            )
            event = AuditEvent(
                seq=seq, timestamp=timestamp, event_type=event_type,
                session_id=session_id, node=node, payload=payload,
                prev_hash=self._last_hash, hash=event_hash,
            )
            self._events.append(event)
            self._seq = seq
            self._last_hash = event_hash
        log.info(
            "audit event appended",
            extra={"event_type": event_type, "seq": seq, "session_id": session_id,
                   "node": node, "hash_prefix": event_hash[:12]},
        )
        return event

    def iter_events(self) -> Iterator[AuditEvent]:
        """Yield stored events in chain order.

        Yields from a shallow *copy* of the event list, so a concurrent
        append during iteration cannot mutate the sequence being walked.
        ``AuditEvent`` forbids extra fields but is not frozen — treat
        returned events as read-only by convention (no backend ever
        mutates a stored event, that is what the chain hash guards).

        Yields:
            :class:`AuditEvent` objects in ascending ``seq`` order.
        """
        events = list(self._events)
        log.debug("iterating in-memory ledger events", extra={"events": len(events)})
        yield from events

    def close(self) -> None:
        """No resources to release.

        Idempotent no-op kept for protocol symmetry with the persistent
        backends; logged at DEBUG so lifecycle traces stay complete.
        """
        log.debug("InMemoryLedger closed (no resources held)")
