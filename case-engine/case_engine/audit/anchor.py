"""External head-hash anchoring for audit ledgers (G10).

The hash chain proves that the ledger is *self-consistent*; an attacker
with write access to the file can still rewrite the whole chain and
re-anchor at genesis (the ``_replay`` tail read trusts the file's first
record).  External anchoring closes that hole: the chain's head hash is
periodically committed to a sink that lives **outside** the ledger's
trust boundary — a second file, a Git ref, an RFC 3161 timestamp
authority, an S3 object-lock bucket — so a full rewrite becomes
detectable by comparing the anchored head with the file's actual head.

This module defines the sink contract plus two dependency-free sinks.
Ambitious sinks (TSA, Git, S3) are small classes implementing the same
one-method protocol; see ``docs/EXTENDING.md`` for a worked example.

Contents
--------
- :class:`AnchorSink` — structural protocol (``write(record)``).
- :class:`FileAnchorSink` — append-only JSONL anchor file (fsync'd).
- :class:`MemoryAnchorSink` — in-memory sink for tests.

Cross-references: ``docs/AUDIT_LEDGER.md`` §5 (lifecycle & anchoring),
``case_engine/audit/jsonl_ledger.py`` (rotation/anchor call-sites),
``docs/GAP_ANALYSIS.md`` G10 (origin).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from case_engine.logging_setup import get_logger

__all__ = ["AnchorSink", "FileAnchorSink", "MemoryAnchorSink", "make_anchor_record"]

log = get_logger(__name__)


def make_anchor_record(
    head_hash: str,
    head_seq: int,
    source_path: str | Path,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Build one anchoring record (pure; shared by ledgers and sinks).

    Args:
        head_hash: Chain tail hash to commit externally.
        head_seq: Sequence number of the last event covered.
        source_path: Ledger (segment) the head belongs to.
        schema_version: AuditEvent schema version covered by the chain.

    Returns:
        JSON-safe anchor record with an ``anchored_at`` UTC stamp.

    Example:
        >>> r = make_anchor_record("ab" * 32, 7, "audit.jsonl")
        >>> r["head_seq"], len(r["head_hash"])
        (7, 64)
    """
    record = {
        "kind": "case-engine-chain-anchor",
        "head_hash": head_hash,
        "head_seq": int(head_seq),
        "source_path": str(source_path),
        "schema_version": int(schema_version),
        "anchored_at": datetime.now(UTC).isoformat(),
    }
    log.debug("anchor record built",
              extra={"head_seq": record["head_seq"],
                     "head_hash_prefix": head_hash[:12]})
    return record


@runtime_checkable
class AnchorSink(Protocol):
    """Structural contract for external anchor stores (G10).

    Implementations must be durable *before* returning (the anchor is
    only as strong as its own persistence) and thread-safe; failures
    raise ``OSError``-family exceptions which the calling ledger wraps
    into :class:`~case_engine.exceptions.LedgerError` (fail-closed — an
    anchoring that silently no-ops would be worse than none).
    """

    def write(self, record: dict[str, Any]) -> None:
        """Persist one anchor record durably.

        Args:
            record: JSON-safe anchor record (see :func:`make_anchor_record`).

        Raises:
            OSError: When the record cannot be persisted (propagates to
                the ledger, which fails closed).
        """
        ...  # pragma: no cover - protocol


class FileAnchorSink:
    """Append-only JSONL anchor file (one record per line, fsync'd).

    The simplest external sink: a second file on different storage (or
    in a different trust domain — e.g. committed to Git, shipped to
    object storage, or merely chmod'd read-only for the engine user).
    Thread-safe via an in-process lock.

    Args:
        path: Anchor file path (created with parents on first write).

    Example:
        >>> import tempfile, os
        >>> sink = FileAnchorSink(os.path.join(tempfile.mkdtemp(), "anchors.jsonl"))
        >>> sink.write(make_anchor_record("ab" * 32, 1, "audit.jsonl"))
    """

    def __init__(self, path: str | Path) -> None:
        """Store the target path (no I/O until first write).

        Args:
            path: Anchor file path.
        """
        self._path = Path(path)
        self._lock = threading.Lock()
        log.info("FileAnchorSink configured", extra={"path": str(self._path)})

    @property
    def path(self) -> Path:
        """Return the anchor file path (for operators and verifiers)."""
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        """Append one anchor record as a JSON line (fsync'd).

        Args:
            record: JSON-safe anchor record.

        Raises:
            OSError: When the file cannot be written — logged at ERROR
                with the traceback before propagating (fail-closed).
        """
        line = _dump_record(record)
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError:
            log.error("anchor write failed",
                      extra={"path": str(self._path)}, exc_info=True)
            raise
        log.info("chain head anchored",
                 extra={"path": str(self._path),
                        "head_seq": record.get("head_seq"),
                        "head_hash_prefix": str(record.get("head_hash", ""))[:12]})


class MemoryAnchorSink:
    """In-memory anchor sink for tests and dry-run verification.

    Example:
        >>> sink = MemoryAnchorSink()
        >>> sink.write(make_anchor_record("ab" * 32, 2, "audit.jsonl"))
        >>> len(sink.records)
        1
    """

    def __init__(self) -> None:
        """Initialise with an empty record list (never raises)."""
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        """Append the record to :attr:`records` (never raises).

        Args:
            record: JSON-safe anchor record.
        """
        self.records.append(dict(record))
        log.debug("anchor stored in memory",
                  extra={"head_seq": record.get("head_seq")})


def _dump_record(record: dict[str, Any]) -> str:
    """Serialize one anchor record deterministically (module helper).

    Args:
        record: JSON-safe dict.

    Returns:
        Compact single-line JSON (``default=str`` fallback keeps the
        sink total for exotic values).
    """
    import json

    return json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
