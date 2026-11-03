"""Optional FastAPI serving layer (G16, v2.3.0) — ``case_engine[server]``.

Purpose & role
--------------
The engine ships as a library + CLI; this module adds the thin HTTP
surface integrators kept asking for, *without* making FastAPI a
dependency of the core (the import happens inside :func:`create_app`,
so ``import case_engine.server`` is safe on any install).

Endpoints
---------
=========================  ===============================================
Method / path               Behavior
=========================  ===============================================
``GET /health``            Version + registry size (cheap, no LLM call).
``POST /sessions``         Run one query through the full pipeline via
                           :meth:`CASEEngine.run_async` (G5 async — the
                           event loop is never blocked by the pipeline).
``GET /sessions/{id}``     Fetch a previously-run session's result from
                           the in-process mirror (bounded, oldest-evicted).
``GET /ledger/verify``     Hash-chain verification of the engine's audit
                           ledger (tamper evidence over HTTP).
=========================  ===============================================

Error mapping (fail-closed preserved over HTTP):

* :class:`ToolCallRejectedError` / :class:`SchemaViolationError` → 422
  (the LLM payload was rejected by the enforcement gate);
* :class:`LLMProviderUnavailableError` → 503 (transient upstream);
* other :class:`CASEError` → 500 with the engine's own message;
* anything else → 500 with a generic message (no internals leak).

Log-safety: request logging carries method, path, session id and
query *length* — never query text, prompt content or PII.

Usage
-----
    uvicorn --factory case_engine.server:create_app --port 8000

or programmatically::

    from case_engine.server import create_app
    app = create_app()  # default config: mock provider, JSONL ledger

Cross-references
----------------
``Dockerfile`` / ``docker-compose.yml`` (container story),
``pyproject.toml`` (the ``server`` extra), ``docs/LLM_PROVIDERS.md``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from case_engine.exceptions import (
    CASEError,
    LLMProviderUnavailableError,
    SchemaViolationError,
)
from case_engine.logging_setup import get_logger
from case_engine.schemas.pipeline import CASEResult
from case_engine.version import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from case_engine.engine import CASEEngine

__all__ = ["create_app"]

log = get_logger(__name__)

#: Maximum number of results kept in the in-process session mirror.
_MIRROR_CAPACITY = 1024


class _SessionMirror:
    """Thread-safe, bounded ``session_id -> CASEResult`` map (FIFO evict).

    Why it exists: ``GET /sessions/{id}`` needs a lookup surface even
    when the engine is configured without a persistent
    :class:`~case_engine.storage.ResultStore`.  The mirror is a
    process-local convenience, never the system of record — the audit
    ledger remains authoritative (same rule as the G12 store).
    """

    def __init__(self, capacity: int = _MIRROR_CAPACITY) -> None:
        """Create an empty mirror with the given capacity."""
        self._capacity = capacity
        self._lock = threading.Lock()
        self._data: OrderedDict[str, CASEResult] = OrderedDict()

    def put(self, result: CASEResult) -> None:
        """Store *result*, evicting the oldest entry when full."""
        with self._lock:
            self._data[result.session_id] = result
            self._data.move_to_end(result.session_id)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def get(self, session_id: str) -> CASEResult | None:
        """Return the result for *session_id* or ``None``."""
        with self._lock:
            return self._data.get(session_id)


class RunRequest(BaseModel):
    """Request body for ``POST /sessions`` (typed, ``extra=forbid``).

    Module-level on purpose (not nested in :func:`create_app`):
    FastAPI resolves endpoint annotations with ``get_type_hints`` in
    module globals, and pydantic is a hard engine dependency, so this
    model does not violate the lazy-FastAPI design.
    """

    model_config = {"extra": "forbid"}

    query: str = Field(..., min_length=1, max_length=20_000,
                       description="User problem statement")
    domain_hint: str | None = Field(
        None, description="Preset routing domain (skips node_1)")
    forced_model_ids: list[str] | None = Field(
        None, description="Force models (bypasses affinity routing)")
    tee_attested: bool | None = Field(
        None, description="Per-call TEE attestation override")


def create_app(
    engine: CASEEngine | None = None,
    *,
    mirror_capacity: int = _MIRROR_CAPACITY,
) -> Any:
    """Build the FastAPI application around a C.A.S.E. engine.

    FastAPI is imported **lazily** so the core package never needs it;
    call this only in deployments that opted into ``case_engine[server]``.
    (The request model below lives at module level and uses pydantic,
    which *is* a hard engine dependency — this keeps FastAPI's type
    resolution working under ``from __future__ import annotations``.)

    Args:
        engine: The engine to serve.  ``None`` builds a default one
            (mock provider + JSONL ledger — fully offline).
        mirror_capacity: Capacity of the in-process session mirror.

    Returns:
        A configured :class:`fastapi.FastAPI` application.

    Raises:
        ImportError: When FastAPI (or its deps) is not installed —
            logged at ERROR first, with the exact pip command in the
            message.

    Example:
        >>> from case_engine.server import create_app  # doctest: +SKIP
        >>> app = create_app()                         # doctest: +SKIP
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        log.error("fastapi requested but not installed", exc_info=True)
        raise ImportError(
            "The serving layer requires the 'server' extra: "
            'pip install "case-engine[server]" '
            "(or pip install fastapi uvicorn)"
        ) from exc

    if engine is None:
        from case_engine.engine import CASEEngine

        engine = CASEEngine()
        log.info("server built default engine",
                 extra={"provider": engine.config.provider})

    mirror = _SessionMirror(mirror_capacity)

    app = FastAPI(
        title="C.A.S.E. engine",
        description="Deterministic, schema-gated mental-model reasoning "
                    "(audit-chained, fail-closed).",
        version=__version__,
    )

    @app.middleware("http")
    async def _log_requests(request: Any, call_next: Any) -> Any:
        """One DEBUG line per request — path only, never bodies."""
        log.debug("http request", extra={"method": request.method,
                                         "path": request.url.path})
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness + engine identity (no LLM call, always cheap)."""
        return {
            "status": "ok",
            "engine_version": __version__,
            "provider": engine.config.provider,
            "models": len(engine.registry),
        }

    @app.post("/sessions", status_code=201)
    async def run_session(body: RunRequest) -> JSONResponse:
        """Run one query through the full pipeline (async path, G5).

        The result is stored in the in-process mirror and (when the
        engine is configured with a result store) persisted durably.
        """
        log.info(
            "session requested",
            extra={"query_len": len(body.query),
                   "domain_hint": body.domain_hint,
                   "forced": bool(body.forced_model_ids)},
        )
        try:
            result = await engine.run_async(
                body.query,
                domain_hint=body.domain_hint,
                forced_model_ids=body.forced_model_ids,
                tee_attested=body.tee_attested,
            )
        except (SchemaViolationError, LLMProviderUnavailableError) as exc:
            status = 422 if isinstance(exc, SchemaViolationError) else 503
            log.warning("session rejected", extra={"status": status,
                                                   "error_type": type(exc).__name__})
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except CASEError as exc:
            log.error("session failed with CASEError",
                      extra={"error_type": type(exc).__name__}, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            log.error("session failed unexpectedly",
                      extra={"error_type": type(exc).__name__}, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="internal engine failure (see server logs)",
            ) from exc
        mirror.put(result)
        return JSONResponse(status_code=201, content=result.model_dump())

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        """Fetch a previously-run session's result from the mirror."""
        result = mirror.get(session_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="session not found (mirror is process-local; "
                       "configure result_store_backend='sqlite' for durable history)",
            )
        return result.model_dump()

    @app.get("/ledger/verify")
    async def verify_ledger() -> dict[str, Any]:
        """Verify the engine's audit-ledger hash chain on demand."""
        verification = engine.verify_audit_chain()
        return verification.model_dump()

    log.info(
        "C.A.S.E. server ready",
        extra={"provider": engine.config.provider,
               "mirror_capacity": mirror_capacity},
    )
    return app



def _uvicorn_entry() -> None:  # pragma: no cover - manual server run
    """Run a local dev server (``python -m case_engine.server``)."""
    import uvicorn  # lazy: optional dependency

    uvicorn.run("case_engine.server:create_app", factory=True,
                host="127.0.0.1", port=8000)


if __name__ == "__main__":  # pragma: no cover
    _uvicorn_entry()
