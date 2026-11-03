"""Append-only JSONL audit ledger (human-readable, grep-able, git-friendly).

One JSON object per line, in chain order.  On construction the ledger
replays any existing file so an audit trail written by a previous
process continues seamlessly (seq and hash linkage preserved).

The append-time integrity protocol
----------------------------------
Each :meth:`JSONLLedger.append` executes, under an exclusive lock, the
fixed sequence:

1. **read tail** — the in-memory chain state (``_seq`` / ``_last_hash``)
   established at open time by ``_replay`` (and, when
   ``cross_process_safe=True``, refreshed from the file's last line
   under the advisory file lock);
2. **hash** — ``compute_event_hash(prev_hash, seq, ...)`` over the
   canonical form of the new event;
3. **write** — one ``AuditEvent.model_dump_json()`` line appended to the
   file and flushed;
4. **fsync** — the file descriptor is fsync'd (unless ``fsync=False``),
   so the line survives process or machine crash before the call
   returns.

Only *after* the durable write do the in-memory chain state and the
returned event advance.  An ``OSError`` during append is **raised,
never swallowed** (wrapped as :class:`~case_engine.exceptions.LedgerError`,
fail-closed: an unauditable session must not continue), and because the
state update is skipped on failure the chain can continue cleanly from
the last persisted hash.

Cross-process safety (G9)
-------------------------
With ``cross_process_safe=True`` (the default) every append acquires an
advisory ``flock`` (``fcntl.LOCK_EX``) on the ledger file and re-reads
the file's last line under the lock before computing the next hash, so
two processes appending to one file serialize correctly and each adopts
the other's tail instead of forking the chain.  On platforms without
``fcntl`` (Windows) the flag degrades to the legacy single-writer
behaviour with a one-time DEBUG note — use the SQLite backend for
multi-writer deployments there.  With ``cross_process_safe=False`` the
ledger is single-writer by assumption (in-process lock only) and skips
the extra tail-read I/O.

Segment rotation and external anchoring (G10)
---------------------------------------------
With ``max_bytes`` set, an append that would overflow the file first
rotates it: the active file is renamed to ``<stem>.seg<N><suffix>``
(N = 1, 2, …), the chain re-anchors at genesis in a fresh active file
(seq restarts at 1 — chains are unbroken *per segment*), and, when an
``anchor_sink`` is configured, an anchor record commits the closed
segment's head hash to that external sink.  :meth:`verify_chain`
verifies the active segment; :meth:`verify_all_segments` walks every
rotated segment plus the active one; :meth:`iter_all_events` yields
across segments oldest→newest.  :meth:`anchor` commits the *current*
head hash on demand (e.g. from a cron job) — this is what elevates the
chain from "self-consistent" to "externally provable".

Invariants
----------
* Corrupt (non-JSON) lines are rejected at open time — corruption must
  be visible, never silently skipped.
* Thread-safe: appends are serialized by an in-process lock; with
  ``cross_process_safe=True`` also cross-process safe (advisory flock).
* No update/delete API — the ledger is append-only by construction.

Cross-references: ``docs/AUDIT_LEDGER.md`` §1–5 (chain format, backend
table, verification, flock semantics, rotation & anchoring),
``case_engine/audit/anchor.py`` (sink contract) and
``tests/unit/test_ledgers.py`` / ``tests/unit/test_p1_gaps.py``.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from case_engine.audit.anchor import make_anchor_record
from case_engine.audit.base import (
    AUDIT_EVENT_SCHEMA_VERSION,
    AuditEvent,
    AuditLedger,
    compute_event_hash,
)
from case_engine.exceptions import LedgerError
from case_engine.logging_setup import get_logger

try:  # pragma: no cover - platform probe
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

__all__ = ["JSONLLedger"]

log = get_logger(__name__)

#: Window (bytes) read from the end of the file when refreshing the
#: chain tail under the cross-process lock (G9).  Covers any single
#: audit line comfortably; pathological single lines larger than this
#: fall back to a full-file tail scan.
_TAIL_WINDOW_BYTES = 65_536


def _read_tail(path: Path) -> dict[str, Any] | None:
    """Return the last complete JSON record of *path* (module helper).

    Reads at most :data:`_TAIL_WINDOW_BYTES` from the end of the file
    (cheap for any realistic ledger) and parses the last non-empty
    complete line.  A full-file scan is the fallback for oversized
    single records.

    Args:
        path: Ledger file to inspect.

    Returns:
        The parsed last record, or ``None`` for an empty/absent file.

    Raises:
        LedgerError: When the tail line is not valid JSON (corruption
            must be visible; logged at ERROR first).
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - _TAIL_WINDOW_BYTES))
            window = fh.read()
    except OSError as exc:
        log.error("cannot read ledger tail",
                  extra={"path": str(path)}, exc_info=True)
        raise LedgerError(f"Cannot read ledger tail {path}: {exc}") from exc
    lines = [ln for ln in window.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Tail window may have split the last line; retry with a full scan.
        try:
            with path.open("r", encoding="utf-8") as fh:
                last = None
                for line in fh:
                    if line.strip():
                        last = line
                return json.loads(last) if last else None
        except (OSError, json.JSONDecodeError, ValueError) as exc2:
            log.error("corrupt ledger tail detected",
                      extra={"path": str(path)}, exc_info=True)
            raise LedgerError(f"Corrupt ledger tail in {path}: {exc2}") from exc2


class JSONLLedger(AuditLedger):
    """File-backed hash-chained ledger (one JSON object per line).

    Concurrency: in-process thread-safe (a lock serializes append and
    close); with ``cross_process_safe=True`` also safe for multiple
    *processes* appending to one file (advisory flock + tail re-read
    under the lock — G9).  Persistence: fsync per append by default —
    the most crash-resistant shipped backend, at the cost of one disk
    flush per event.

    Args:
        path: Ledger file path (created on first append).
        fsync: Durably flush to disk on every append (crash-safe; disable
            only for high-throughput benchmarks).
        payload_cipher: Optional :class:`~case_engine.audit.crypto.PayloadCipher`
            (G4b).  When provided, every payload is encrypted into an
            AESGCM envelope **before** hashing, so the chain commits
            to the ciphertext exactly as persisted (encrypt-then-
            chain); ``verify_chain`` therefore needs no key, and
            :meth:`iter_events` decrypts only on request.  A cipher
            failure during append fails the session closed.
        cross_process_safe: Acquire an advisory ``flock`` per append and
            re-read the file tail under the lock so multiple processes
            can share one ledger file (G9).  Default ``True``; set to
            ``False`` to restore the cheapest single-writer behaviour.
        max_bytes: Size-based rotation threshold (G10).  ``None`` (the
            default) disables rotation; a positive value rotates the
            active file to ``<stem>.seg<N><suffix>`` before the
            overflow append and re-anchors the chain at genesis.
        anchor_sink: Optional :class:`~case_engine.audit.anchor.AnchorSink`
            (G10).  When configured, each rotation and each
            :meth:`anchor` call commits the segment head hash to the
            external sink.

    Raises:
        LedgerError: If an existing file contains a non-JSON line
            (corruption must be visible, never silently skipped), or if
            the parent directory cannot be created / the file cannot be
            read.  All cases are logged at ERROR (with ``exc_info``)
            before raising.

    Example:
        Hermetic example — the ledger is created inside a throwaway
        temp directory so the run never touches (or re-anchors from)
        a pre-existing ``audit.jsonl`` in the working directory, and
        logging is silenced so INFO milestones cannot pollute output:

        >>> from case_engine.logging_setup import configure_logging
        >>> _ = configure_logging(level="CRITICAL")
        >>> import tempfile, os
        >>> path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        >>> ledger = JSONLLedger(path)
        >>> event = ledger.append("SESSION_START", "s-1", "engine", {"v": 1})
        >>> event.seq
        1
        >>> ledger.verify_chain().valid
        True
        >>> ledger.close()                      # doctest: +SKIP
    """

    def __init__(
        self,
        path: str | Path,
        fsync: bool = True,
        payload_cipher: Any | None = None,
        cross_process_safe: bool = True,
        max_bytes: int | None = None,
        anchor_sink: Any | None = None,
    ) -> None:
        """Open (or create) the ledger and replay any existing chain.

        Logs INFO on success (with the number of replayed events) and a
        WARNING when the chain re-anchors at genesis because no file
        exists yet — the operator-visible signal that a *new* audit
        trail is starting rather than continuing one.

        Args:
            path: Ledger file path (created on first append).
            fsync: Durably flush to disk on every append.
            payload_cipher: Optional payload cipher (G4b; see class doc).
            cross_process_safe: Advisory-flock appends (G9; see class doc).
            max_bytes: Rotation threshold (G10; ``None`` = disabled).
            anchor_sink: External anchor sink (G10; optional).

        Raises:
            LedgerError: On corrupt existing content (non-JSON line) or
                unwritable/unreadable location — logged ERROR before
                raising, with the underlying ``OSError`` chained.
            ValueError: When ``max_bytes`` is set but not positive
                (logged at ERROR first).
        """
        if max_bytes is not None and max_bytes <= 0:
            log.error("invalid rotation threshold",
                      extra={"max_bytes": max_bytes})
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        self._path = Path(path)
        self._fsync = fsync
        self._cipher = payload_cipher
        self._cross_process_safe = cross_process_safe and _FCNTL_AVAILABLE
        if cross_process_safe and not _FCNTL_AVAILABLE:
            log.debug(
                "fcntl unavailable - cross_process_safe disabled "
                "(use the SQLite backend for multi-writer deployments)",
                extra={"path": str(self._path)},
            )
        self._max_bytes = max_bytes
        self._anchor_sink = anchor_sink
        self._lock = threading.Lock()
        self._handle: TextIO | None = None
        self._segment_index = 0  # 0 = active file; 1..n = rotated segments
        self._flock_warned = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._discover_segments()
            self._replay()
        except LedgerError:
            raise
        except OSError as exc:
            log.error("cannot open JSONL ledger file",
                      extra={"path": str(self._path)}, exc_info=True)
            raise LedgerError(f"Cannot open ledger file {self._path}: {exc}") from exc
        log.info(
            "JSONL ledger opened",
            extra={"path": str(self._path), "events": self._seq,
                   "segment_index": self._segment_index,
                   "cross_process_safe": self._cross_process_safe,
                   "rotation": self._max_bytes, "anchoring": self._anchor_sink is not None},
        )

    # ------------------------------------------------------------------ #
    # Segment discovery & replay
    # ------------------------------------------------------------------ #
    def _segment_path(self, index: int) -> Path:
        """Return the file path for segment *index* (0 = active file).

        Args:
            index: Segment index (0 = active, 1..n = rotated).

        Returns:
            ``path`` itself for index 0, else ``<stem>.seg<N><suffix>``.
        """
        if index == 0:
            return self._path
        return self._path.with_name(f"{self._path.stem}.seg{index}{self._path.suffix}")

    def _discover_segments(self) -> None:
        """Set ``_segment_index`` to the highest existing rotated segment.

        Pure filesystem scan (glob over ``<stem>.seg*<suffix>``); keeps
        rotation numbering monotonic across restarts so a reopened
        ledger never overwrites an earlier segment.
        """
        highest = 0
        parent = self._path.parent
        for candidate in parent.glob(f"{self._path.stem}.seg*{self._path.suffix}"):
            stem = candidate.stem  # e.g. "audit.seg3"
            tail = stem.rsplit(".seg", 1)[-1]
            if tail.isdigit():
                highest = max(highest, int(tail))
        self._segment_index = highest
        if highest:
            log.debug("rotated segments discovered",
                      extra={"path": str(self._path), "highest_segment": highest})

    def _replay(self) -> None:
        """Restore ``_seq``/``_last_hash`` from the active segment.

        Reads the file line by line and re-anchors the in-memory chain
        state on the last persisted record, so appends continue the
        stored chain instead of restarting from genesis.  The file is
        opened read-only; the append handle is opened lazily on first
        append.

        Raises:
            LedgerError: On unreadable or corrupt (non-JSON) content —
                logged ERROR (with ``exc_info``) before raising.
        """
        if not self._path.exists():
            log.warning(
                "no existing ledger file; audit chain re-anchored at genesis",
                extra={"path": str(self._path)},
            )
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        log.error("corrupt ledger line detected",
                                  extra={"path": str(self._path)}, exc_info=True)
                        raise LedgerError(
                            f"Corrupt ledger line in {self._path}: {exc}"
                        ) from exc
                    self._seq = int(record["seq"])
                    self._last_hash = record["hash"]
        except OSError as exc:
            log.error("cannot read JSONL ledger file",
                      extra={"path": str(self._path)}, exc_info=True)
            raise LedgerError(f"Cannot read ledger file {self._path}: {exc}") from exc
        if self._seq:
            log.debug("chain tail read",
                      extra={"path": str(self._path), "seq": self._seq,
                             "hash_prefix": str(self._last_hash)[:12]})

    # ------------------------------------------------------------------ #
    # Rotation & anchoring (G10)
    # ------------------------------------------------------------------ #
    def _maybe_rotate(self, incoming_bytes: int) -> None:
        """Rotate the active segment when the append would overflow it.

        Called under the append lock.  On rotation: the append handle is
        closed, the active file renamed to the next ``.seg<N>`` name,
        the chain state re-anchored at genesis, and (when configured) an
        anchor record for the closed segment written to the external
        sink **before** the new segment accepts events — an anchor that
        lags its segment is useless.

        Args:
            incoming_bytes: Size of the line about to be appended.

        Raises:
            LedgerError: On any rename/write failure (fail-closed; the
                session must not continue on an unrotatable ledger).
        """
        if self._max_bytes is None:
            return
        try:
            current = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            current = 0
        # An empty active file can never be improved by rotating (and
        # rotating it would spin out empty segments when a single line
        # exceeds max_bytes) — accept the oversize line instead.
        if current == 0 or current + incoming_bytes <= self._max_bytes:
            return
        next_index = self._segment_index + 1
        target = self._segment_path(next_index)
        closed_head = self._last_hash
        closed_seq = self._seq
        try:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if self._path.exists():
                self._path.rename(target)
        except OSError as exc:
            log.error("ledger rotation failed (fail-closed)",
                      extra={"path": str(self._path), "target": str(target)},
                      exc_info=True)
            raise LedgerError(f"Ledger rotation failed: {exc}") from exc
        self._segment_index = next_index
        self._seq = 0
        self._last_hash = "0" * 64
        log.warning(
            "audit ledger rotated",
            extra={"closed_segment": str(target), "closed_seq": closed_seq,
                   "closed_head_prefix": closed_head[:12],
                   "active_path": str(self._path), "max_bytes": self._max_bytes},
        )
        if self._anchor_sink is not None:
            record = make_anchor_record(
                closed_head, closed_seq, target,
                schema_version=AUDIT_EVENT_SCHEMA_VERSION,
            )
            record["event"] = "rotate"
            self._anchor_sink.write(record)

    def anchor(self, note: str | None = None) -> dict[str, Any] | None:
        """Commit the current chain head to the external sink (G10).

        The operator-facing entry point for periodic anchoring (cron,
        session end, deployment hook).  With no sink configured the call
        is a logged no-op returning ``None``.  The record is exactly
        what an external verifier compares against the file head.

        Args:
            note: Optional free-form note stored in the anchor record
                (deployment id, cron run id …).  Never prompt content.

        Returns:
            The anchor record as written (or ``None`` without a sink).

        Raises:
            LedgerError: When the sink write fails (fail-closed —
                anchoring failures must be visible, logged at ERROR
                first with the underlying error chained).
        """
        if self._anchor_sink is None:
            log.debug("anchor requested without sink - no-op",
                      extra={"path": str(self._path)})
            return None
        record = make_anchor_record(
            self._last_hash, self._seq, self._path,
            schema_version=AUDIT_EVENT_SCHEMA_VERSION,
        )
        record["event"] = "manual"
        if note:
            record["note"] = note
        try:
            self._anchor_sink.write(record)
        except OSError as exc:
            log.error("external anchoring failed (fail-closed)",
                      extra={"path": str(self._path)}, exc_info=True)
            raise LedgerError(f"Ledger anchoring failed: {exc}") from exc
        log.info("chain head anchored externally",
                 extra={"path": str(self._path), "head_seq": self._seq,
                        "head_hash_prefix": self._last_hash[:12]})
        return record

    # ------------------------------------------------------------------ #
    # Append
    # ------------------------------------------------------------------ #
    def append(
        self,
        event_type: str,
        session_id: str,
        node: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """Append one event under an exclusive lock.

        Follows the read-tail → hash → write → fsync protocol documented
        in the module docstring: the in-memory chain state advances only
        after the line is durably on disk, so a failed append leaves the
        chain continuable and the session fails closed.  With
        ``cross_process_safe=True`` the tail is re-read under an
        advisory flock first (G9) and an externally advanced chain is
        adopted rather than forked; rotation (G10) runs before the
        write when the line would overflow ``max_bytes``.

        Args:
            event_type: Stable event-type string.
            session_id: Owning session.
            node: Emitting node.
            payload: JSON-safe body (never logged; only its field count).

        Returns:
            The chained :class:`AuditEvent`.

        Raises:
            LedgerError: On any I/O failure, payload encryption failure
                when a cipher is configured, lock contention with
                another writer, or a failed rotation/anchor write.
                Logged at ERROR with the traceback and file context
                before raising; the underlying error is chained, never
                swallowed.
        """
        log.debug(
            "ledger append requested",
            extra={"event_type": event_type, "session_id": session_id,
                   "node": node, "payload_fields": len(payload)},
        )
        with self._lock:
            locked = False
            try:
                # G9: acquire the advisory flock BEFORE reading the tail so
                # concurrent writers serialize across processes; the tail
                # refresh then adopts any externally advanced chain instead
                # of forking it.
                if self._cross_process_safe:
                    if self._handle is None:
                        self._handle = self._path.open("a", encoding="utf-8")
                    try:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                        locked = True
                    except OSError as exc:
                        log.error(
                            "advisory flock unavailable (fail-closed)",
                            extra={"path": str(self._path)}, exc_info=True,
                        )
                        raise LedgerError(
                            f"Ledger file lock could not be acquired: {exc}"
                        ) from exc
                    self._refresh_tail_under_lock()
                # G4b: encrypt-then-chain — hash and persist exactly the same
                # (envelope) payload so verification stays key-free.
                try:
                    stored_payload = (
                        self._cipher.encrypt(payload) if self._cipher is not None else payload
                    )
                except Exception as exc:
                    log.error(
                        "payload encryption failed (fail-closed)",
                        extra={"path": str(self._path), "event_type": event_type,
                               "session_id": session_id},
                        exc_info=True,
                    )
                    raise LedgerError(f"Ledger payload encryption failed: {exc}") from exc
                timestamp = datetime.now(UTC).isoformat()
                # G10: rotate BEFORE the sequence/hash computation so a
                # fresh segment starts clean at seq=1 / genesis.  The line
                # size is estimated (payload + fixed envelope overhead);
                # a single event larger than max_bytes gets its own
                # segment rather than an error.
                estimate = len(json.dumps(
                    stored_payload, ensure_ascii=False, default=str,
                ).encode("utf-8")) + 256
                self._maybe_rotate(estimate)
                seq = self._seq + 1
                event_hash = compute_event_hash(
                    self._last_hash, seq, timestamp, event_type, session_id, node, stored_payload
                )
                event = AuditEvent(
                    seq=seq, timestamp=timestamp, event_type=event_type,
                    session_id=session_id, node=node, payload=stored_payload,
                    prev_hash=self._last_hash, hash=event_hash,
                    schema_version=AUDIT_EVENT_SCHEMA_VERSION,
                )
                line = event.model_dump_json() + "\n"
                try:
                    if self._handle is None:
                        self._handle = self._path.open("a", encoding="utf-8")
                    self._handle.write(line)
                    self._handle.flush()
                    if self._fsync:
                        os.fsync(self._handle.fileno())
                except OSError as exc:
                    log.error(
                        "ledger append failed (fail-closed)",
                        extra={"path": str(self._path), "seq": seq,
                               "event_type": event_type, "session_id": session_id},
                        exc_info=True,
                    )
                    raise LedgerError(f"Ledger append failed: {exc}") from exc
                self._seq = seq
                self._last_hash = event_hash
            finally:
                if locked and self._handle is not None:
                    try:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                    except OSError:  # pragma: no cover - close will release anyway
                        log.debug("flock release failed (handle close releases it)",
                                  extra={"path": str(self._path)})
        log.info(
            "audit event appended",
            extra={"event_type": event_type, "seq": seq, "session_id": session_id,
                   "node": node, "path": str(self._path),
                   "hash_prefix": event_hash[:12]},
        )
        return event

    def _refresh_tail_under_lock(self) -> None:
        """Adopt an externally advanced chain tail (G9 helper).

        Called under both the in-process lock and the advisory file
        flock.  Reads the file's last record; when the file's tail seq
        is ahead of the in-memory state, the external tail is adopted
        (INFO log) so the next hash links to it.  A *behind* file tail
        (should not happen while we hold the write lock) is ignored.

        Raises:
            LedgerError: When the tail cannot be read or is corrupt
                (propagated from :func:`_read_tail`; fail-closed).
        """
        if self._handle is not None:
            try:
                self._handle.flush()
            except OSError:  # pragma: no cover - defensive
                pass
        tail = _read_tail(self._path)
        if tail is None:
            return
        tail_seq = int(tail.get("seq", 0) or 0)
        tail_hash = str(tail.get("hash", ""))
        if tail_seq > self._seq and tail_hash:
            log.info(
                "adopting externally advanced ledger tail",
                extra={"path": str(self._path), "file_seq": tail_seq,
                       "memory_seq": self._seq,
                       "hash_prefix": tail_hash[:12]},
            )
            self._seq = tail_seq
            self._last_hash = tail_hash

    # ------------------------------------------------------------------ #
    # Reading (active + all segments)
    # ------------------------------------------------------------------ #
    def iter_events(self, decrypt: bool = False) -> Iterator[AuditEvent]:
        """Yield every event of the **active segment** in chain order.

        Each call takes a fresh snapshot: events appended after the
        iteration started (including by re-opened handles) are visible
        on the next call.  Malformed lines that were valid at open time
        would surface here as ``json.JSONDecodeError`` /
        ``pydantic.ValidationError`` — the open-time replay is the
        corruption gate.  Use :meth:`iter_all_events` for rotated
        history.

        Args:
            decrypt: When a payload cipher is configured and *decrypt* is
                ``True``, AESGCM envelopes are decrypted before yielding
                (G4b); a wrong key or tampered ciphertext raises
                :class:`LedgerError` (fail-closed).  The default
                ``False`` yields stored (envelope) payloads — sufficient
                for chain verification and key-free export.

        Yields:
            :class:`AuditEvent` objects in ascending ``seq`` order.

        Raises:
            LedgerError: Only when ``decrypt=True`` and a payload cannot
                be authenticated/decrypted (logged at ERROR first).
        """
        if not self._path.exists():
            log.debug("iterating empty JSONL ledger", extra={"path": str(self._path)})
            return
        log.debug("iterating JSONL ledger events",
                  extra={"path": str(self._path), "decrypt": decrypt})
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    event = AuditEvent.model_validate(json.loads(line))
                    if decrypt and self._cipher is not None:
                        try:
                            plain = self._cipher.decrypt(event.payload)
                        except Exception as exc:
                            log.error(
                                "payload decryption failed during iteration",
                                extra={"path": str(self._path), "seq": event.seq},
                                exc_info=True,
                            )
                            raise LedgerError(
                                f"Ledger payload decryption failed at seq={event.seq}: {exc}"
                            ) from exc
                        event = event.model_copy(update={"payload": plain})
                    yield event

    def iter_all_events(self, decrypt: bool = False) -> Iterator[AuditEvent]:
        """Yield events across **all** segments, oldest segment first (G10).

        Rotated segments (``<stem>.seg1``, ``.seg2``, …) are walked in
        ascending index order, then the active file.  Segment boundaries
        are visible as fresh genesis anchors (``seq == 1`` with
        ``prev_hash == GENESIS_HASH``).

        Args:
            decrypt: Passed through to :meth:`iter_events`.

        Yields:
            :class:`AuditEvent` objects across the whole ledger history.

        Raises:
            LedgerError: Propagated from per-segment iteration (I/O or
                decryption failure; fail-closed).
        """
        for index in range(0, self._segment_index + 1):
            segment = self._segment_path(index)
            if not segment.exists():
                continue
            log.debug("iterating ledger segment",
                      extra={"segment": str(segment), "index": index})
            yield from self._iter_segment(segment, decrypt=decrypt)

    def _iter_segment(self, segment: Path, decrypt: bool) -> Iterator[AuditEvent]:
        """Yield the events of one segment file (module helper).

        Args:
            segment: Segment file path.
            decrypt: Whether to decrypt payloads (G4b cipher only).

        Yields:
            Parsed :class:`AuditEvent` objects.

        Raises:
            LedgerError: On undecryptable payloads (fail-closed).
        """
        with segment.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = AuditEvent.model_validate(json.loads(line))
                if decrypt and self._cipher is not None:
                    try:
                        plain = self._cipher.decrypt(event.payload)
                    except Exception as exc:
                        log.error(
                            "payload decryption failed during segment iteration",
                            extra={"segment": str(segment), "seq": event.seq},
                            exc_info=True,
                        )
                        raise LedgerError(
                            f"Ledger payload decryption failed in {segment} "
                            f"at seq={event.seq}: {exc}"
                        ) from exc
                    event = event.model_copy(update={"payload": plain})
                yield event

    def verify_all_segments(self) -> list[Any]:
        """Verify every segment's chain, oldest first (G10).

        Each segment is an independent genesis-anchored chain; the
        glue between segments is the external anchor records written by
        rotation/:meth:`anchor`.

        Returns:
            One :class:`~case_engine.audit.base.ChainVerification` per
            existing segment, ordered ``seg1 … segN, active``.  Empty
            list when no segment exists yet.
        """
        results = []
        for index in range(0, self._segment_index + 1):
            segment = self._segment_path(index)
            if not segment.exists():
                continue
            log.debug("verifying ledger segment",
                      extra={"segment": str(segment), "index": index})
            verification = self._verify_segment(segment)
            results.append(verification)
            if not verification.valid:
                log.warning("segment chain verification FAILED",
                            extra={"segment": str(segment),
                                   "first_invalid_seq": verification.first_invalid_seq})
        total_valid = all(v.valid for v in results)
        log.info("segment sweep finished",
                 extra={"segments": len(results), "all_valid": total_valid})
        return results

    def _verify_segment(self, segment: Path) -> Any:
        """Verify one segment file's chain (module helper).

        Args:
            segment: Segment file path.

        Returns:
            :class:`ChainVerification` for the segment (``valid=True``
            and ``total_entries=0`` for an absent file).
        """
        expected_prev = "0" * 64
        expected_seq = 1
        total = 0
        if not segment.exists():
            from case_engine.audit.base import ChainVerification

            return ChainVerification(valid=True, total_entries=0)
        with segment.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = AuditEvent.model_validate(json.loads(line))
                total += 1
                if event.seq != expected_seq or event.prev_hash != expected_prev:
                    from case_engine.audit.base import ChainVerification

                    return ChainVerification(
                        valid=False, total_entries=total,
                        first_invalid_seq=event.seq,
                        reason=f"Segment {segment.name}: linkage break at seq={event.seq}",
                    )
                recomputed = compute_event_hash(
                    event.prev_hash, event.seq, event.timestamp, event.event_type,
                    event.session_id, event.node, event.payload,
                )
                if recomputed != event.hash:
                    from case_engine.audit.base import ChainVerification

                    return ChainVerification(
                        valid=False, total_entries=total,
                        first_invalid_seq=event.seq,
                        reason=f"Segment {segment.name}: hash mismatch at seq={event.seq}",
                    )
                expected_prev = event.hash
                expected_seq += 1
        from case_engine.audit.base import ChainVerification

        return ChainVerification(valid=True, total_entries=total)

    def close(self) -> None:
        """Close the append handle (idempotent).

        Multiple close calls are safe (subsequent calls see the handle
        already reset).  Note the handle re-opens lazily: appending
        after :meth:`close` silently reopens the file and continues the
        chain rather than raising.

        Raises:
            OSError: Only in the rare case the underlying
                ``TextIOBase.close`` fails; propagated unchanged
                (deliberate — closing must not mask state).
        """
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None
                log.info("JSONL ledger closed", extra={"path": str(self._path)})
