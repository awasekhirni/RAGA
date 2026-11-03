"""Validation subpackage: rules, judge, and the ValidationEngine (node_5).

Security role
-------------
Content-level half of the engine's zero-trust stack (the schema-level
half lives in :mod:`case_engine.enforcement`).  After the Pydantic gate
admits a payload, the components here decide whether the *reasoning*
inside it satisfies the mental-model contract:

* :mod:`case_engine.validation.rules` -- the 16 deterministic rules
  (VR001–VR016): pure functions, offline-capable, fail-closed on crash.
* :mod:`case_engine.validation.llm_judge` -- OPTIONAL adversarial
  LLM-as-judge veto for the five semantic rules; degrades fail-open to
  heuristic-only on judge failure (the deterministic layer never does).
* :class:`ValidationEngine` -- orchestrates schema parse + rule battery
  per trace and emits the authoritative
  :class:`~case_engine.schemas.reports.TraceValidationReport`.

Invariants
----------
- A trace that did not pass here is invisible to downstream nodes.
- Rule crashes fail the *trace*, never the pipeline (fail-closed);
  validation infrastructure errors raise rather than fake a pass.
- The judge can veto, never rescue.

Contents
--------
- ``ValidationEngine`` (from :mod:`case_engine.validation.engine`)
- ``LLMBackedJudge``, ``LLMJudge``, ``JudgeVerdict``
  (from :mod:`case_engine.validation.llm_judge`)
- ``VALIDATORS``, ``run_rule`` (from :mod:`case_engine.validation.rules`)

Cross-references
----------------
docs/VALIDATION_RULES.md (per-rule semantics), docs/PIPELINE.md
(node_5 placement), docs/ARCHITECTURE.md §1.3 (fail-closed rationale).
"""

from __future__ import annotations

from case_engine.validation.engine import ValidationEngine
from case_engine.validation.llm_judge import JudgeVerdict, LLMBackedJudge, LLMJudge
from case_engine.validation.rules import VALIDATORS, run_rule

__all__ = ["ValidationEngine", "LLMBackedJudge", "LLMJudge", "JudgeVerdict",
           "VALIDATORS", "run_rule"]
