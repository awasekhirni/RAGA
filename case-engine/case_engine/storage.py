"""First-class session-result persistence (G12).

``CASEResult`` objects are returned and printed, but the only durable
record of a session has been the audit ledger — whose payloads are
digests and evidence fragments, deliberately not queryable session
records.  This module adds an optional :class:`ResultStore` so "show me
last Tuesday's sessions" is one query instead of a full-ledger scan.

Design rules
------------
* **Opt-in**: the default backend is ``"none"`` (legacy behaviour, zero
  I/O).  Enable via ``EngineConfig(result_store_backend=...)`` or by
  injecting a store into :class:`~case_engine.engine.CASEEngine`.
* **Store, not evidence**: the result store is an operational index —
  the hash-chained ledger remains the tamper-evident source of truth.
  Stores are best-effort: a save failure logs at ERROR and never fails
  the session (contrast with the fail-closed ledger).
* **Same trio pattern as the ledger**: an abstract-ish protocol, a
  :class:`SQLiteResultStore` (the shipped persistent backend) and an
  :class:`InMemoryResultStore` (tests/CI), plus a ``create_result_store``
  factory keyed like ``create_ledger``.
* **JSON-safe rows**: results are persisted as canonical
  ``model_dump_json`` documents with projected query columns
  (``session_id``, ``started_at``, ``finished_at``, ``all_valid``,
  ``escalated``, ``domain``) so both humans and SQL see the same data.

Contents
--------
- :class:`ResultStore` — structural protocol.
- :class:`InMemoryResultStore` — dict-backed store for tests.
- :class:`SQLiteResultStore` — file-backed store (``sqlite3`` stdlib).
- :func:`create_result_store` — backend-key factory.

Cross-references: ``docs/GAP_ANALYSIS.md`` G12 (origin),
``case_engine/engine.py`` (the save point),
``case_engine/cli.py`` (``case-engine history``), ``docs/PIPELINE.md``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from case_engine.exceptions import ConfigurationError, StorageError
from case_engine.logging_setup import get_logger
from case_engine.schemas.pipeline import CASEResult

__all__ = ["ResultStore", "InMemoryResultStore", "SQLiteResultStore",
           "create_result_store"]

log = get_logger(__name__)


@runtime_checkable
class ResultStore(Protocol):
    """Structural contract for session-result stores (G12).

    Implementations must be thread-safe and must *never* raise for a
    save that fails (log ERROR, return) — the store is best-effort by
    contract; the ledger is the authoritative record.
    """

    def save(self, result: CASEResult) -> bool:
        """Persist one result (idempotent per ``session_id``).

        Args:
            result: The completed session result.

        Returns:
            ``True`` when stored; ``False`` when the save failed (logged).
        """
        ...  # pragma: no cover - protocol

    def get(self, session_id: str) -> CASEResult | None:
        """Fetch one result by session id.

        Args:
            session_id: The session to fetch.

        Returns:
            The stored result, or ``None`` when absent.
        """
        ...  # pragma: no cover - protocol

    def iter_since(
        self,
        since_iso: str | None = None,
        limit: int | None = None,
    ) -> Iterator[CASEResult]:
        """Yield stored results newest-first, optionally filtered.

        Args:
            since_iso: ISO-8601 lower bound on ``started_at`` (inclusive);
                ``None`` = no filter.
            limit: Maximum number of results; ``None`` = unlimited.

        Yields:
            :class:`CASEResult` objects in descending ``started_at``.
        """
        ...  # pragma: no cover - protocol

    def close(self) -> None:
        """Release backend resources (idempotent)."""
        ...  # pragma: no cover - protocol


class InMemoryResultStore:
    """Dict-backed store for tests, demos and single-process tools.

    Example:
        >>> from case_engine.schemas.pipeline import CASEResult
        >>> s = InMemoryResultStore()
        >>> s.save(CASEResult(session_id="s1", user_query="q"))
        True
        >>> s.get("s1").session_id
        's1'
    """

    def __init__(self) -> None:
        """Initialise an empty store (never raises)."""
        self._results: dict[str, CASEResult] = {}

    def save(self, result: CASEResult) -> bool:
        """Store the result keyed by ``session_id`` (never raises).

        Args:
            result: The result to store.

        Returns:
            ``True`` (in-memory saves cannot fail).
        """
        self._results[result.session_id] = result
        log.debug("result stored", extra={"backend": "memory",
                                          "session_id": result.session_id})
        return True

    def get(self, session_id: str) -> CASEResult | None:
        """Return the stored result or ``None``.

        Args:
            session_id: Session to fetch.

        Returns:
            The stored :class:`CASEResult` or ``None``.
        """
        return self._results.get(session_id)

    def iter_since(
        self,
        since_iso: str | None = None,
        limit: int | None = None,
    ) -> Iterator[CASEResult]:
        """Yield results newest-first by ``started_at``.

        Args:
            since_iso: Inclusive ISO lower bound (string compare on the
                ISO-8601 field, which is monotonic for UTC stamps).
            limit: Cap on yielded count.

        Yields:
            :class:`CASEResult` objects.
        """
        rows = [r for r in self._results.values()
                if since_iso is None or r.started_at >= since_iso]
        rows.sort(key=lambda r: r.started_at, reverse=True)
        if limit is not None:
            rows = rows[: max(0, limit)]
        yield from rows

    def close(self) -> None:
        """Clear the store (idempotent; never raises)."""
        self._results.clear()


class SQLiteResultStore:
    """File-backed result store (stdlib ``sqlite3``, WAL-friendly).

    One row per session; the full ``CASEResult`` JSON document rides in
    a column while projected fields index the common queries.  Saves
    are best-effort: failures log at ERROR and return ``False`` (the
    session's result still reaches the caller).

    Args:
        path: Database file (created with parents on first use).

    Raises:
        StorageError: When the database file cannot be opened/created
            (logged at ERROR first, underlying error chained).

    Example:
        >>> import tempfile, os
        >>> store = SQLiteResultStore(os.path.join(tempfile.mkdtemp(), "r.sqlite3"))
        >>> store.save(CASEResult(session_id="s2", user_query="q"))
        True
        >>> store.get("s2").user_query
        'q'
    """

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the results database.

        Args:
            path: Database file path.

        Raises:
            StorageError: On open/connect failure — logged at ERROR
                with the traceback before raising.
        """
        self._path = Path(path)
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS case_results (
                    session_id  TEXT PRIMARY KEY,
                    started_at  TEXT NOT NULL,
                    finished_at TEXT,
                    all_valid   INTEGER NOT NULL DEFAULT 0,
                    escalated   INTEGER NOT NULL DEFAULT 0,
                    domain      TEXT,
                    document    TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_case_results_started "
                "ON case_results(started_at)"
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.error("cannot open result store",
                      extra={"path": str(self._path)}, exc_info=True)
            raise StorageError(
                f"Cannot open result store {self._path}: {exc}"
            ) from exc
        log.info("SQLiteResultStore opened", extra={"path": str(self._path)})

    def save(self, result: CASEResult) -> bool:
        """Upsert the result row (best-effort; never raises).

        Args:
            result: The result to persist.

        Returns:
            ``True`` on success; ``False`` on a database error (logged
            at ERROR with the traceback first).
        """
        document = result.model_dump_json()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO case_results
                        (session_id, started_at, finished_at, all_valid,
                         escalated, domain, document)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        started_at=excluded.started_at,
                        finished_at=excluded.finished_at,
                        all_valid=excluded.all_valid,
                        escalated=excluded.escalated,
                        domain=excluded.domain,
                        document=excluded.document
                    """,
                    (result.session_id, result.started_at, result.finished_at,
                     int(result.all_valid), int(result.escalated),
                     result.domain_classification, document),
                )
                self._conn.commit()
        except sqlite3.Error:
            log.error("result store save failed (best-effort)",
                      extra={"path": str(self._path),
                             "session_id": result.session_id},
                      exc_info=True)
            return False
        log.debug("result stored", extra={"backend": "sqlite",
                                          "session_id": result.session_id})
        return True

    def get(self, session_id: str) -> CASEResult | None:
        """Fetch one result by id (``None`` when absent).

        Args:
            session_id: Session to fetch.

        Returns:
            The parsed :class:`CASEResult` or ``None``.
        """
        try:
            row = self._conn.execute(
                "SELECT document FROM case_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            log.error("result store read failed",
                      extra={"path": str(self._path), "session_id": session_id},
                      exc_info=True)
            return None
        if row is None:
            return None
        return CASEResult.model_validate(json.loads(row[0]))

    def iter_since(
        self,
        since_iso: str | None = None,
        limit: int | None = None,
    ) -> Iterator[CASEResult]:
        """Yield results newest-first (SQL-backed projection).

        Args:
            since_iso: Inclusive lower bound on ``started_at``.
            limit: Cap on yielded count.

        Yields:
            Parsed :class:`CASEResult` objects.

        Raises:
            StorageError: Only on an unexpected database failure mid-
                iteration (logged at ERROR first).
        """
        query = "SELECT document FROM case_results"
        params: list[Any] = []
        if since_iso is not None:
            query += " WHERE started_at >= ?"
            params.append(since_iso)
        query += " ORDER BY started_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        try:
            cursor = self._conn.execute(query, params)
            for (document,) in cursor:
                yield CASEResult.model_validate(json.loads(document))
        except sqlite3.Error as exc:
            log.error("result store query failed",
                      extra={"path": str(self._path)}, exc_info=True)
            raise StorageError(f"Result store query failed: {exc}") from exc

    def close(self) -> None:
        """Close the connection (idempotent; never raises)."""
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:  # pragma: no cover - close is forgiving
            log.debug("result store close raised (ignored)",
                      extra={"path": str(self._path)})
        log.info("SQLiteResultStore closed", extra={"path": str(self._path)})


def create_result_store(backend: str, path: str | None = None) -> ResultStore | None:
    """Instantiate a result store by key (G12 factory).

    Args:
        backend: ``"none"`` | ``"memory"`` | ``"sqlite"``.
        path: File path for ``"sqlite"``; defaults to
            ``./case_results.sqlite3``.  Ignored otherwise.

    Returns:
        The store instance, or ``None`` for ``"none"`` (legacy
        behaviour: results are returned but not persisted).

    Raises:
        ConfigurationError: On an unknown backend key — logged at ERROR
            with the available keys before raising.

    Example:
        >>> store = create_result_store("memory")
        >>> store.save(CASEResult(session_id="s3", user_query="q"))
        True
        >>> create_result_store("none") is None
        True
    """
    if backend == "none":
        log.debug("result persistence disabled (backend=none)")
        return None
    factories: dict[str, Any] = {
        "memory": lambda _p: InMemoryResultStore(),
        "sqlite": lambda p: SQLiteResultStore(p or "case_results.sqlite3"),
    }
    if backend not in factories:
        log.error("unknown result store backend requested",
                  extra={"backend": backend, "available": sorted(factories)})
        raise ConfigurationError(f"Unknown result store backend {backend!r}",
                                 available=sorted(factories))
    log.debug("instantiating result store backend",
              extra={"backend": backend, "path": path})
    return factories[backend](path)
