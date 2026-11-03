"""SQLite-backed audit ledger (queryable, single-file, ACID appends).

Same chain contract as the JSONL backend, stored in a single SQLite
database: one row per :class:`~case_engine.audit.base.AuditEvent` in the
``audit_events`` table (schema frozen — the hash chain covers exactly
these columns), plus indexes on ``session_id`` and ``event_type`` for
analytics (reflexion hotspots, session timelines, SIEM exports).

Invariants
----------
* Each ``append`` is a single autocommitted INSERT (``isolation_level=None``
  ⇒ one statement = one ACID transaction): either the row is durable or
  the in-memory chain state does not advance and the failure surfaces as
  :class:`~case_engine.exceptions.LedgerError` (fail-closed).
* The chain continues from the last persisted row on reopen (``_replay``);
  reopening is the operator's tamper-check entry point because the
  verification sweep re-reads stored rows.
* No UPDATE/DELETE is ever issued by this backend — append-only by
  construction (the SQL schema still permits external writes; that is
  exactly what :meth:`verify_chain` exists to detect).

Concurrency semantics
---------------------
* In-process: a ``threading.Lock`` serializes appends and close;
  ``check_same_thread=False`` lets the connection be used across
  threads *under that lock*.
* Cross-process: SQLite file locking makes concurrent writers safe but
  contended; a single writer process per ledger file is recommended.
  WAL mode is intentionally not enabled (the default rollback journal
  keeps the file maximally portable).

Contents
--------
- :class:`SQLiteLedger` — the SQL-backed implementation of
  :class:`~case_engine.audit.base.AuditLedger`.

Cross-references: ``docs/AUDIT_LEDGER.md`` §2 (backend table), §4
(event vocabulary + ready-made SQL queries) and
``tests/unit/test_ledgers.py`` (SQL tamper detection via external
UPDATE).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from case_engine.audit.base import AuditEvent, AuditLedger, compute_event_hash
from case_engine.exceptions import LedgerError
from case_engine.logging_setup import get_logger

__all__ = ["SQLiteLedger"]

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq         INTEGER PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    node        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_type    ON audit_events(event_type);
"""


class SQLiteLedger(AuditLedger):
    """SQLite ledger with the same chain contract as :class:`JSONLLedger`.

    Useful when operators want SQL queries over the trail, e.g.::

        SELECT event_type, COUNT(*) FROM audit_events
        WHERE session_id = ? GROUP BY event_type;

    Concurrency: thread-safe appends via the in-process lock (see module
    docstring for cross-process semantics).  Persistence: every append
    is an autocommitted, durable INSERT.

    Args:
        path: Database file path; ``":memory:"`` keeps it in RAM.

    Raises:
        LedgerError: On any open/creation failure (missing parent
            directory, unwritable path, schema error).  Logged at ERROR
            with ``exc_info`` before raising; the ``sqlite3.Error`` is
            chained.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        payload_cipher: Any | None = None,
    ) -> None:
        """Open the database, ensure the schema, replay the chain head.

        Logs INFO on success (with the number of replayed events) and
        DEBUG when the chain tail was restored from a stored row.

        Args:
            path: Database file path; ``":memory:"`` keeps it in RAM
                (parent-directory creation is skipped for it).
            payload_cipher: Optional
                :class:`~case_engine.audit.crypto.PayloadCipher` (G4b).
                When provided, payloads are encrypted into AESGCM
                envelopes before hashing/persistence (encrypt-then-chain)
                and :meth:`iter_events` decrypts only on request.

        Raises:
            LedgerError: When the connection cannot be opened or the
                schema cannot be applied — logged ERROR before raising.
        """
        self._path = str(path)
        self._cipher = payload_cipher
        self._lock = threading.Lock()
        try:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self._path, check_same_thread=False, isolation_level=None
            )
            self._conn.executescript(_SCHEMA)
            self._replay()
        except sqlite3.Error as exc:
            log.error("cannot open SQLite ledger",
                      extra={"path": self._path}, exc_info=True)
            raise LedgerError(f"Cannot open SQLite ledger {self._path}: {exc}") from exc
        log.info("SQLite ledger opened", extra={"path": self._path, "events": self._seq})

    def _replay(self) -> None:
        """Restore ``_seq``/``_last_hash`` from stored rows.

        Reads only the chain tail (``ORDER BY seq DESC LIMIT 1``) — the
        full chain is never re-derived here; use :meth:`verify_chain`
        for that.  An empty table leaves the genesis anchor in place.

        Raises:
            LedgerError: On SQL failure — logged ERROR with ``exc_info``
                before raising.
        """
        try:
            row = self._conn.execute(
                "SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            log.error("SQLite ledger replay failed",
                      extra={"path": self._path}, exc_info=True)
            raise LedgerError(f"Ledger replay failed: {exc}") from exc
        if row:
            self._seq, self._last_hash = int(row[0]), row[1]
            log.debug("chain tail read",
                      extra={"path": self._path, "seq": self._seq,
                             "hash_prefix": str(self._last_hash)[:12]})

    def append(
        self,
        event_type: str,
        session_id: str,
        node: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """Append one chained event inside an exclusive lock.

        The row is written as one autocommitted INSERT (module
        docstring, "Invariants"); the in-memory chain state advances
        only after the INSERT succeeds, so a failed append leaves the
        chain continuable and the session fails closed.

        Args:
            event_type: Stable event-type string.
            session_id: Owning session.
            node: Emitting node.
            payload: JSON-safe body (stored as compact JSON; never
                logged — only its field count).

        Returns:
            The chained :class:`AuditEvent`.

        Raises:
            LedgerError: On SQL failure (constraint violation, locked
                database, disk error).  Logged at ERROR with the
                traceback and row context before raising; the
                ``sqlite3.Error`` is chained, never swallowed.
        """
        import json as _json

        log.debug(
            "ledger append requested",
            extra={"event_type": event_type, "session_id": session_id,
                   "node": node, "payload_fields": len(payload)},
        )
        with self._lock:
            # G4b: encrypt-then-chain — hash and store exactly the same
            # (envelope) payload so verification stays key-free.
            try:
                stored_payload = (
                    self._cipher.encrypt(payload) if self._cipher is not None else payload
                )
            except Exception as exc:
                log.error(
                    "payload encryption failed (fail-closed)",
                    extra={"path": self._path, "event_type": event_type,
                           "session_id": session_id},
                    exc_info=True,
                )
                raise LedgerError(f"Ledger payload encryption failed: {exc}") from exc
            seq = self._seq + 1
            timestamp = datetime.now(UTC).isoformat()
            event_hash = compute_event_hash(
                self._last_hash, seq, timestamp, event_type, session_id, node, stored_payload
            )
            event = AuditEvent(
                seq=seq, timestamp=timestamp, event_type=event_type,
                session_id=session_id, node=node, payload=stored_payload,
                prev_hash=self._last_hash, hash=event_hash,
            )
            try:
                self._conn.execute(
                    "INSERT INTO audit_events (seq, timestamp, event_type, session_id,"
                    " node, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                    (seq, timestamp, event_type, session_id, node,
                     _json.dumps(stored_payload, ensure_ascii=False, default=str),
                     self._last_hash, event_hash),
                )
            except sqlite3.Error as exc:
                log.error(
                    "ledger append failed (fail-closed)",
                    extra={"path": self._path, "seq": seq,
                           "event_type": event_type, "session_id": session_id},
                    exc_info=True,
                )
                raise LedgerError(f"Ledger append failed: {exc}") from exc
            self._seq = seq
            self._last_hash = event_hash
        log.info(
            "audit event appended",
            extra={"event_type": event_type, "seq": seq, "session_id": session_id,
                   "node": node, "path": self._path,
                   "hash_prefix": event_hash[:12]},
        )
        return event

    def iter_events(self, decrypt: bool = False) -> Iterator[AuditEvent]:
        """Yield every event in chain order (payload JSON re-parsed).

        Rows are streamed from a single forward-only cursor; events
        appended while iterating are not guaranteed to appear until the
        next call.

        Args:
            decrypt: When a payload cipher is configured and ``True``,
                AESGCM envelopes are decrypted before yielding (G4b);
                authentication failures raise :class:`LedgerError`
                (fail-closed).  Default ``False`` yields stored payloads.

        Yields:
            :class:`AuditEvent` objects in ascending ``seq`` order.

        Raises:
            LedgerError: Only when ``decrypt=True`` and a payload cannot
                be authenticated/decrypted.

        Note:
            Without ``decrypt=True`` this generator never raises
            :class:`LedgerError` itself; iterating after :meth:`close`
            surfaces as ``sqlite3.ProgrammingError`` (the lock-free read
            path mirrors the original behaviour).
        """
        import json as _json

        log.debug("iterating SQLite ledger events",
                  extra={"path": self._path, "decrypt": decrypt})
        cursor = self._conn.execute(
            "SELECT seq, timestamp, event_type, session_id, node, payload,"
            " prev_hash, hash FROM audit_events ORDER BY seq ASC"
        )
        for seq, ts, etype, sid, node, payload, prev_hash, ev_hash in cursor:
            event = AuditEvent(
                seq=seq, timestamp=ts, event_type=etype, session_id=sid,
                node=node, payload=_json.loads(payload),
                prev_hash=prev_hash, hash=ev_hash,
            )
            if decrypt and self._cipher is not None:
                try:
                    plain = self._cipher.decrypt(event.payload)
                except Exception as exc:
                    log.error(
                        "payload decryption failed during iteration",
                        extra={"path": self._path, "seq": event.seq},
                        exc_info=True,
                    )
                    raise LedgerError(
                        f"Ledger payload decryption failed at seq={event.seq}: {exc}"
                    ) from exc
                event = event.model_copy(update={"payload": plain})
            yield event

    def close(self) -> None:
        """Close the connection (idempotent).

        After closing, ``append``/``iter_events`` raise
        ``sqlite3.ProgrammingError`` (mirrors the original behaviour —
        the engine closes ledgers only at session teardown).

        Raises:
            sqlite3.Error: Only in the rare case the underlying
                connection close fails; propagated unchanged.
        """
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None  # type: ignore[assignment]
                log.info("SQLite ledger closed", extra={"path": self._path})
