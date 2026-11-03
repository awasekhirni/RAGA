"""Base contract shared by every mental-model trace schema.

Purpose & role
--------------
``BaseTrace`` gives all 15 mental-model Pydantic classes:

* strict validation (``extra='forbid'`` — an LLM that invents fields is
  rejected, per the zero-trust posture),
* whitespace stripping on strings,
* a universal ``confidence`` field (audited by VR016),
* a UTC ISO-8601 ``timestamp`` with timezone offset (the spec used the
  deprecated ``datetime.utcnow()``; this implementation is tz-aware).

Contents
--------
* :class:`BaseTrace` — common Pydantic foundation for the 15 tool
  contracts defined in :mod:`case_engine.schemas.mental_models`.
* :func:`utc_now_iso` — tz-aware timestamp default factory.

Invariants
----------
* Every ``BaseTrace`` subclass is an *LLM tool contract*: the class name
  doubles as the LLM tool name (Strategy 1 of ``docs/ARCHITECTURE.md``),
  so renaming a subclass requires a coordinated update of the registry
  data modules (``case_engine/registry/model_data_a.py``,
  ``model_data_b.py``) and of ``SCHEMA_BY_MODEL_ID``.
* ``confidence`` is mandatory on every trace and audited against the
  owning model's ``confidence_threshold`` by rule VR016 (executable
  heuristic in ``case_engine/validation/rules.py``).
* Traces are produced by pipeline node_4 (generation), validated at
  node_5, and re-checked server-side on every tool call by the
  enforcement gate (``case_engine/enforcement/gate.py``).

Cross-references
----------------
* Typed contracts: :mod:`case_engine.schemas.mental_models`
* Pipeline / report contracts: :mod:`case_engine.schemas.pipeline`,
  :mod:`case_engine.schemas.reports`
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/VALIDATION_RULES.md``
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from case_engine.logging_setup import get_logger

log = get_logger(__name__)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Used as the ``default_factory`` of ``BaseTrace.timestamp`` so every
    generated trace carries a creation timestamp without the caller
    having to supply one.  Timezone-aware by design (the spec's
    ``datetime.utcnow()`` is deprecated and produces naive datetimes).

    Returns:
        e.g. ``"2026-01-15T09:30:12.123456+00:00"``.

    Example:
        >>> ts = utc_now_iso()
        >>> ts.endswith("+00:00")
        True
    """
    now = datetime.now(UTC).isoformat()
    log.debug("UTC timestamp generated", extra={"timestamp": now})
    return now


class BaseTrace(BaseModel):
    """Common foundation for all mental-model execution traces.

    Why it exists: it centralises the zero-trust validation posture
    (``extra='forbid'``, whitespace stripping) and the two universal
    trace fields (``confidence``, ``timestamp``) so the 15 concrete
    contracts in :mod:`case_engine.schemas.mental_models` only declare
    their model-specific payload fields.  Instances are produced by
    pipeline node_4 (generation), validated at node_5, and re-validated
    by the enforcement gate on every LLM tool call.

    Attributes:
        confidence: Self-assessed confidence in [0.0, 1.0]; validated
            against the model threshold by rule VR016.
        timestamp: ISO-8601 UTC creation timestamp (auto-filled).

    Example:
        >>> class Demo(BaseTrace):
        ...     answer: str
        >>> d = Demo(answer="ok", confidence=0.9)
        >>> d.timestamp.startswith("2")
        True
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    confidence: float = Field(..., ge=0.0, le=1.0, description="Self-assessed confidence [0,1]")
    timestamp: str = Field(default_factory=utc_now_iso, description="ISO-8601 UTC timestamp")
