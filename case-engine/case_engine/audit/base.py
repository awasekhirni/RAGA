"""Hash-chained audit ledger contracts — the tamper-evidence backbone.

Every reasoning session writes an append-only event chain where each
entry commits to its predecessor:

``hash_n = SHA-256(prev_hash ‖ seq ‖ timestamp ‖ event_type ‖ session_id
‖ node ‖ canonical_json(payload))``

Properties:
* **Tamper-evident** — editing or deleting any historical entry breaks
  every subsequent hash; :meth:`AuditLedger.verify_chain` detects it.
* **Append-only by convention** — backends expose no update/delete.
* **Portable** — the canonical form is the JSON serialization of
  :class:`AuditEvent`, so a JSONL file can be re-imported into SQLite
  and re-verified byte-identically.

Invariants honoured by every backend
------------------------------------
* ``append`` is durable before it returns — the JSONL backend fsyncs
  each line (unless ``fsync=False``), the SQLite backend commits each
  INSERT; the in-memory chain state (``seq`` / ``last hash``) advances
  only *after* the event is persisted.
* The chain continues from the **last persisted hash** — reopening a
  ledger replays the stored tail, so sessions spanning processes form
  one unbroken chain.
* :meth:`AuditLedger.verify_chain` detects any mutation of a single
  event (edit, delete, reorder) and reports the first bad ``seq``.
* Fail-closed: a ledger that cannot append aborts the session rather
  than let it continue unaudited (see ``docs/AUDIT_LEDGER.md`` §6).

Contents
--------
- :data:`GENESIS_HASH` — the ``prev_hash`` anchor of the first event.
- :func:`canonical_json` — deterministic JSON serialization used for
  hashing (sort_keys + compact separators + non-ASCII preserved).
- :func:`compute_event_hash` — one SHA-256 chain step.
- :class:`AuditEvent` — one hash-chained ledger entry (Pydantic).
- :class:`ChainVerification` — result of a verification sweep.
- :class:`AuditLedger` — abstract backend contract implemented by
  :class:`~case_engine.audit.jsonl_ledger.JSONLLedger`,
  :class:`~case_engine.audit.sqlite_ledger.SQLiteLedger` and
  :class:`~case_engine.audit.memory_ledger.InMemoryLedger`.

Cross-references: ``docs/AUDIT_LEDGER.md`` (chain format, backend
selection table, verification CLI, event vocabulary, PII strategy,
DPO dataset extraction) and ``docs/EXTENDING.md``.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from case_engine.logging_setup import get_logger

__all__ = ["GENESIS_HASH", "AuditEvent", "ChainVerification", "AuditLedger",
           "compute_event_hash", "canonical_json"]

log = get_logger(__name__)

#: Chain anchor: the ``prev_hash`` of the very first event.
GENESIS_HASH: str = "0" * 64

#: ``AuditEvent.schema_version`` written by this build (G10).  Bumped
#: only when the event model itself changes shape in a way migration
#: tooling must react to (v2.2.0 added the field with default 1).
AUDIT_EVENT_SCHEMA_VERSION: int = 1


def canonical_json(obj: Any) -> str:
    """Return the deterministic JSON serialization used for hashing.

    Sorts keys, uses compact separators and preserves non-ASCII
    characters so the same logical event hashes identically everywhere.
    Non-serializable values fall back to ``str()`` rather than raising,
    which keeps audit appends total (an audit trail must never fail
    because of an exotic payload value).

    Args:
        obj: Any JSON-serializable object.

    Returns:
        Canonical JSON string.

    Raises:
        ValueError: On circular references (undumpable by nature).
        TypeError: Only for objects whose ``str()`` cannot be
            determined — practically unreachable because ``default=str``
            coerces anything with a ``__str__``.

    Example:
        >>> canonical_json({"b": 1, "a": 2})
        '{"a":2,"b":1}'
    """
    out = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    log.debug("canonical json produced", extra={"type": type(obj).__name__, "chars": len(out)})
    return out


def compute_event_hash(
    prev_hash: str,
    seq: int,
    timestamp: str,
    event_type: str,
    session_id: str,
    node: str,
    payload: dict[str, Any],
) -> str:
    """Compute the SHA-256 chain hash for one event.

    The hash material is the :func:`canonical_json` of a fixed
    seven-field dict (``prev_hash``, ``seq``, ``timestamp``,
    ``event_type``, ``session_id``, ``node``, ``payload``) — changing
    that recipe would invalidate every existing ledger, so treat it as
    a frozen wire format.

    Args:
        prev_hash: Hash of the previous event (genesis for the first).
        seq: 1-based sequence number.
        timestamp: ISO-8601 event time.
        event_type: Stable event-type string.
        session_id: Owning session.
        node: Pipeline node that emitted the event.
        payload: Event body.

    Returns:
        Hex digest string.

    Example:
        >>> len(compute_event_hash(GENESIS_HASH, 1, "t", "E", "s", "n", {"a": 1}))
        64
    """
    material = canonical_json({
        "prev_hash": prev_hash, "seq": seq, "timestamp": timestamp,
        "event_type": event_type, "session_id": session_id,
        "node": node, "payload": payload,
    })
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    # Prefix only — the payload itself is never logged.
    log.debug("event hash computed", extra={"seq": seq, "event_type": event_type,
                                            "hash_prefix": digest[:12]})
    return digest


class AuditEvent(BaseModel):
    """One hash-chained ledger entry.

    The wire format across every backend: JSONL stores exactly
    ``model_dump_json()`` per line; SQLite stores the same fields in
    columns.  ``extra="forbid"`` guarantees no unhashed metadata can
    ride along — every visible field is covered by the chain hash.

    Attributes:
        seq: 1-based position in the ledger (per segment under G10
            rotation).
        timestamp: ISO-8601 UTC creation time.
        event_type: Stable :class:`~case_engine.schemas.pipeline.AuditEventType`
            value string.
        session_id: Owning reasoning session.
        node: Pipeline node (or ``"engine"`` for lifecycle events).
        payload: JSON-safe event body.
        prev_hash: Hash of the previous entry (genesis anchor first;
            per-segment genesis after a G10 rotation).
        hash: This entry's chain hash.
        schema_version: :class:`~case_engine.version.SCHEMA_VERSION`-major
            of the event model (G10 forward-migration field, added in
            v2.2.0; older ledgers replay as version 1).  The chain-hash
            recipe is deliberately frozen, so this field rides *outside*
            the hash — migration routing must therefore treat it as
            advisory and verify tamper-evidence via the per-session
            ``SESSION_START`` payload, which carries the version *inside*
            the hashed material.

    Example:
        >>> ev = AuditEvent(seq=1, timestamp="t", event_type="E",
        ...                 session_id="s", node="n", payload={},
        ...                 prev_hash=GENESIS_HASH, hash="ab" * 32)
        >>> ev.seq, ev.prev_hash == GENESIS_HASH, ev.schema_version
        (1, True, 1)
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    timestamp: str
    event_type: str
    session_id: str
    node: str
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
    schema_version: int = Field(default=1, ge=1)


class ChainVerification(BaseModel):
    """Result of a full-chain verification sweep.

    Attributes:
        valid: ``True`` when every entry links and hashes correctly.
        total_entries: Number of entries scanned.
        first_invalid_seq: Sequence number of the first bad entry
            (``None`` for a pristine ledger).
        reason: Human-readable explanation of the first failure
            (``None`` for a pristine ledger).

    Example:
        >>> ChainVerification(valid=True, total_entries=3).first_invalid_seq is None
        True
    """

    model_config = ConfigDict(extra="forbid")

    valid: bool
    total_entries: int
    first_invalid_seq: int | None = None
    reason: str | None = None


class AuditLedger(ABC):
    """Abstract append-only, hash-chained audit backend.

    Three backends implement this identical contract; pick by
    operational need (see ``docs/AUDIT_LEDGER.md`` §2):

    * :class:`~case_engine.audit.jsonl_ledger.JSONLLedger` — default.
      Human-readable, grep/jq-friendly, git-diffable, fsync'd per
      append.  In-process thread safety via a lock; the file itself is
      the source of truth.
    * :class:`~case_engine.audit.sqlite_ledger.SQLiteLedger` — ACID
      appends and SQL queries for dashboards/SIEM.  Same in-process
      locking; single-writer-per-file is recommended across processes.
    * :class:`~case_engine.audit.memory_ledger.InMemoryLedger` — no
      durability by design; tests, CI and ephemeral demos.

    Attributes:
        _seq: Sequence number of the last appended event (0 = empty
            chain; advanced only after a durable append).
        _last_hash: Chain tail — hash of the last event, genesis anchor
            while the chain is empty.

    Logging contract: implementations log INFO on open/close and on
    every appended event (``event_type`` + ``seq`` + ``session_id``),
    DEBUG for chain-tail reads and hash computations (hash **prefix**
    only — payloads are never logged), and ERROR before any
    :class:`~case_engine.exceptions.LedgerError`.
    """

    _seq: int = 0
    _last_hash: str = GENESIS_HASH

    @abstractmethod
    def append(
        self,
        event_type: str,
        session_id: str,
        node: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """Append one event and return the chained entry.

        Args:
            event_type: Stable event-type string.
            session_id: Owning session.
            node: Emitting node (``"engine"`` for lifecycle events).
            payload: JSON-safe body.

        Returns:
            The persisted :class:`AuditEvent` (with ``seq``/``hash``).

        Raises:
            LedgerError: When the event cannot be durably persisted
                (I/O or SQL failure).  Logged at ERROR with the
                traceback before raising; the in-memory chain state is
                left untouched so a retry continues the same chain —
                the engine treats this as fatal (fail-closed auditing).
        """

    @abstractmethod
    def iter_events(self) -> Iterator[AuditEvent]:
        """Yield every stored event in chain order (seq ascending).

        Implementations must be side-effect free and safe to call
        repeatedly; :meth:`verify_chain` and :meth:`count` are built on
        this primitive.
        """

    @abstractmethod
    def close(self) -> None:
        """Release backend resources (idempotent).

        Safe to call multiple times and on an already-closed backend;
        after closing, ``append``/``iter_events`` behavior is
        backend-defined (JSONL reopens lazily, SQLite raises).
        """

    def verify_chain(self) -> ChainVerification:
        """Re-derive the whole chain and report the first inconsistency.

        Checks, in order: sequence continuity (1..n), prev-hash linkage,
        and per-entry hash recomputation.  Verification stops at the
        **first** failure — everything after a break is untrusted by
        definition — and never raises: the verdict is the return value.

        Returns:
            :class:`ChainVerification` — ``valid=True`` for a pristine
            ledger; otherwise the first bad seq plus the reason.

        Example:
            >>> ledger = InMemoryLedger()             # doctest: +SKIP
            >>> ledger.verify_chain().valid           # doctest: +SKIP
            True
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        total = 0
        log.debug("chain verification sweep started", extra={"backend": type(self).__name__})
        for event in self.iter_events():
            total += 1
            log.debug("verifying chained event",
                      extra={"seq": event.seq, "event_type": event.event_type})
            if event.seq != expected_seq:
                reason = f"Sequence break: expected seq={expected_seq}, found {event.seq}"
                log.warning("audit chain verification FAILED",
                            extra={"first_invalid_seq": event.seq,
                                   "total_entries": total, "reason": reason})
                return ChainVerification(
                    valid=False, total_entries=total, first_invalid_seq=event.seq,
                    reason=reason,
                )
            if event.prev_hash != expected_prev:
                reason = f"Broken link at seq={event.seq}: prev_hash mismatch"
                log.warning("audit chain verification FAILED",
                            extra={"first_invalid_seq": event.seq,
                                   "total_entries": total, "reason": reason})
                return ChainVerification(
                    valid=False, total_entries=total, first_invalid_seq=event.seq,
                    reason=reason,
                )
            recomputed = compute_event_hash(
                event.prev_hash, event.seq, event.timestamp, event.event_type,
                event.session_id, event.node, event.payload,
            )
            if recomputed != event.hash:
                reason = f"Hash mismatch at seq={event.seq}: payload was modified"
                log.warning("audit chain verification FAILED",
                            extra={"first_invalid_seq": event.seq,
                                   "total_entries": total, "reason": reason})
                return ChainVerification(
                    valid=False, total_entries=total, first_invalid_seq=event.seq,
                    reason=reason,
                )
            expected_prev = event.hash
            expected_seq += 1
        log.info("audit chain verified OK", extra={"total_entries": total})
        return ChainVerification(valid=True, total_entries=total)

    def count(self) -> int:
        """Return the number of stored events.

        Returns:
            Total event count (full scan over :meth:`iter_events`).
        """
        log.debug("counting ledger events", extra={"backend": type(self).__name__})
        return sum(1 for _ in self.iter_events())
