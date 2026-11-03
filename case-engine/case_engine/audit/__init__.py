"""Audit subpackage: hash-chained ledger backends + factory.

The audit ledger is the engine's tamper-evidence backbone: an
append-only chain where every
:class:`~case_engine.audit.base.AuditEvent` commits to its predecessor
via ``hash_n = SHA-256(prev_hash ‖ seq ‖ timestamp ‖ event_type ‖
session_id ‖ node ‖ canonical_json(payload))``.  All backends implement
the identical :class:`~case_engine.audit.base.AuditLedger` contract
(``append`` / ``iter_events`` / ``verify_chain`` / ``close``), so
verification and export code are backend-agnostic.

Invariants
----------
* ``append`` is durable before it returns; the chain continues from the
  last persisted hash across process restarts.
* ``verify_chain`` detects any mutation of a single event and reports
  the first bad ``seq``; a ledger failure fails the session closed.
* Fail-closed auditing: a ledger that cannot append aborts the session
  rather than let it continue unaudited.

Contents
--------
- :class:`AuditLedger`, :class:`AuditEvent`, :class:`ChainVerification`,
  :data:`GENESIS_HASH`, :data:`AUDIT_EVENT_SCHEMA_VERSION`,
  :func:`canonical_json`, :func:`compute_event_hash` — the chain
  contracts (``audit.base``).
- :class:`JSONLLedger` — default file backend (fsync per append;
  advisory-flock cross-process appends, rotation and external
  anchoring — G9/G10).
- :class:`SQLiteLedger` — SQL-queryable backend (ACID appends;
  recommended multi-writer backend).
- :class:`InMemoryLedger` — volatile backend for tests/CI.
- :class:`AnchorSink`, :class:`FileAnchorSink`, :class:`MemoryAnchorSink`,
  :func:`make_anchor_record` — external head-hash anchoring (G10,
  ``audit.anchor``).
- :func:`create_ledger` — backend-key factory used by
  :class:`~case_engine.config.EngineConfig`.

Cross-references: ``docs/AUDIT_LEDGER.md`` (full guide: chain format,
backends, verification CLI, event vocabulary, PII strategy, DPO export)
and ``tests/unit/test_ledgers.py`` (tamper-detection proofs).
"""

from __future__ import annotations

from typing import Any

from case_engine.audit.anchor import (
    AnchorSink,
    FileAnchorSink,
    MemoryAnchorSink,
    make_anchor_record,
)
from case_engine.audit.base import (
    AUDIT_EVENT_SCHEMA_VERSION,
    GENESIS_HASH,
    AuditEvent,
    AuditLedger,
    ChainVerification,
    canonical_json,
    compute_event_hash,
)
from case_engine.audit.crypto import AEADPayloadCipher, PayloadCipher
from case_engine.audit.jsonl_ledger import JSONLLedger
from case_engine.audit.memory_ledger import InMemoryLedger
from case_engine.audit.sqlite_ledger import SQLiteLedger
from case_engine.exceptions import ConfigurationError
from case_engine.logging_setup import get_logger

__all__ = [
    "AuditLedger", "AuditEvent", "ChainVerification", "JSONLLedger",
    "SQLiteLedger", "InMemoryLedger", "create_ledger", "compute_event_hash",
    "canonical_json", "GENESIS_HASH", "PayloadCipher", "AEADPayloadCipher",
    "AnchorSink", "FileAnchorSink", "MemoryAnchorSink", "make_anchor_record",
    "AUDIT_EVENT_SCHEMA_VERSION",
]

log = get_logger(__name__)


def create_ledger(
    backend: str,
    path: str | None = None,
    payload_cipher: Any | None = None,
) -> AuditLedger:
    """Instantiate a ledger backend by key.

    The single construction point for configuration-driven setups
    (CLI, ``EngineConfig``); programmatic users may also instantiate
    backend classes directly.

    Args:
        backend: ``"jsonl"`` | ``"sqlite"`` | ``"memory"``.
        path: File path for persistent backends; defaults to
            ``./case_audit.jsonl`` / ``./case_audit.sqlite3``.  Ignored
            for ``"memory"``.
        payload_cipher: Optional at-rest payload cipher (G4b) passed
            through to the JSONL and SQLite backends (encrypt-then-chain:
            payloads become AESGCM envelopes before hashing).  Ignored by
            the memory backend (volatile, nothing persists to encrypt)
            — callers pairing ``ledger_backend="memory"`` with a cipher
            get the cipher silently unused, which the engine logs as a
            WARNING at construction time.

    Returns:
        A ready ledger instance (already opened/replayed; appends start
        continuing any existing chain).

    Raises:
        ConfigurationError: On an unknown backend key — logged at ERROR
            with the available keys before raising.

    Example:
        >>> ledger = create_ledger("memory")
        >>> ledger.append("SESSION_START", "s-1", "engine", {}).seq
        1
        >>> ledger.verify_chain().valid
        True
    """
    factories = {
        "jsonl": lambda p: JSONLLedger(p or "case_audit.jsonl", payload_cipher=payload_cipher),
        "sqlite": lambda p: SQLiteLedger(p or "case_audit.sqlite3", payload_cipher=payload_cipher),
        "memory": lambda _p: InMemoryLedger(),
    }
    if backend not in factories:
        log.error(
            "unknown ledger backend requested",
            extra={"backend": backend, "available": sorted(factories)},
        )
        raise ConfigurationError(f"Unknown ledger backend {backend!r}",
                                 available=sorted(factories))
    log.debug("instantiating ledger backend",
              extra={"backend": backend, "path": path,
                     "encrypted": payload_cipher is not None})
    return factories[backend](path)
