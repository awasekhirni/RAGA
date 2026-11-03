"""Enforcement subpackage: the Pydantic gate + provider tool-spec factory.

Security role
-------------
This subpackage implements Strategy 1 of the C.A.S.E. spec — schema-gated
tool invocation under a zero-trust posture:

* :class:`SchemaGate` re-validates every LLM tool-call payload against
  the authoritative typed contract (``extra="forbid"``) immediately
  before a pipeline node consumes it.  It is the single point where
  generated content is admitted into engine state — the engine never
  trusts prior validation, its own or the provider's.
* :class:`ToolFactory` derives provider tool specs from the same typed
  classes, so what the provider is *allowed* to emit and what the gate
  *accepts* can never drift — and no registry string is ever executed
  (no ``exec``/``eval`` anywhere in the engine).

Invariants
----------
- Fail-closed: schema rejections are structured ``GateDecision`` data
  (retryable via reflexion); only enforcement faults — unknown tool
  names, unresolvable contracts — raise, and those are logged at ERROR
  first.
- Payload content is never logged above DEBUG; log extras carry tool
  names, error strings and counters only.

Contents
--------
- ``SchemaGate`` (from :mod:`case_engine.enforcement.gate`)
- ``ToolFactory`` (from :mod:`case_engine.enforcement.tool_factory`)

Cross-references
----------------
docs/ARCHITECTURE.md §1.1–§1.3 (strategy mapping, typed contracts,
fail-closed rationale); docs/VALIDATION_RULES.md (the semantic rule
battery that runs after the gate); docs/EXTENDING.md (adding models).
"""

from __future__ import annotations

from case_engine.enforcement.gate import SchemaGate
from case_engine.enforcement.tool_factory import ToolFactory

__all__ = ["SchemaGate", "ToolFactory"]
