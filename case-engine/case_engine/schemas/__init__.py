"""Schemas subpackage: mental-model tool contracts + pipeline/report models.

Purpose & role
--------------
Every Pydantic/TypedDict contract used by the C.A.S.E. engine lives here,
re-exported at this level so engine code and integrators can
``from case_engine.schemas import ...`` without knowing the layout.

Contents
--------
* :mod:`case_engine.schemas.base` — ``BaseTrace`` foundation and the
  tz-aware timestamp factory.
* :mod:`case_engine.schemas.mental_models` — the 15 LLM tool contracts
  (MM001–MM015) plus the ``SCHEMA_BY_MODEL_ID`` index.
* :mod:`case_engine.schemas.pipeline` — ``CASEState`` / ``CASEResult``
  and the domain/phase/audit-event enums.
* :mod:`case_engine.schemas.reports` — gate and validation report models.

Invariants
----------
* The 15 contract class names double as LLM tool names (Strategy 1 of
  ``docs/ARCHITECTURE.md``) — never rename without a coordinated update
  of ``SCHEMA_BY_MODEL_ID`` and the registry data modules.
* Model ids MM001–MM015 and rule ids VR001–VR016 are stable identifiers
  that appear in audit records — never rename.
* All contracts stay JSON-safe so pipeline state round-trips through
  ledgers, logs and the optional LangGraph adapter.

Cross-references
----------------
* Loader / source of truth: :mod:`case_engine.registry`.
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/VALIDATION_RULES.md``,
  ``docs/PIPELINE.md``, ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

from case_engine.schemas.base import BaseTrace, utc_now_iso
from case_engine.schemas.mental_models import (
    SCHEMA_BY_MODEL_ID,
    ComplexityDomainClassifier,
    ConsequenceMappingTree,
    EmotionalRegulationFilter,
    EvolutionaryArchitecturePlan,
    FirstPrinciplesDeconstruction,
    InversionPreMortem,
    LegacyRefactoringAudit,
    MetricGamingPrevention,
    OccamHypothesisRanking,
    OODAExecutionTrace,
    ParetoOptimizationPlan,
    RootCauseAnalysisTrace,
    SixHatsEvaluation,
    SociotechnicalAlignmentCheck,
    TelemetryDriftValidation,
)
from case_engine.schemas.pipeline import (
    AuditEventType,
    CASEResult,
    CASEState,
    CynefinDomain,
    PipelinePhase,
    SpecialistDomain,
)
from case_engine.schemas.reports import (
    GateDecision,
    RuleReport,
    TraceRecord,
    TraceValidationReport,
)

__all__ = [
    "BaseTrace", "utc_now_iso", "SCHEMA_BY_MODEL_ID",
    "OODAExecutionTrace", "FirstPrinciplesDeconstruction", "SixHatsEvaluation",
    "ParetoOptimizationPlan", "RootCauseAnalysisTrace", "OccamHypothesisRanking",
    "EmotionalRegulationFilter", "EvolutionaryArchitecturePlan", "InversionPreMortem",
    "LegacyRefactoringAudit", "ConsequenceMappingTree", "SociotechnicalAlignmentCheck",
    "ComplexityDomainClassifier", "MetricGamingPrevention", "TelemetryDriftValidation",
    "CASEState", "CASEResult", "AuditEventType", "CynefinDomain", "PipelinePhase",
    "SpecialistDomain", "GateDecision", "RuleReport", "TraceRecord",
    "TraceValidationReport",
]
