"""The 15 authoritative mental-model Pydantic contracts (MM001–MM015).

Purpose & role
--------------
Each class below is the **executable** version of the corresponding
``pydantic_schema`` string in the cognitive registry — the engine always
validates against these typed classes, never against the spec strings
(which are retained for interop only).  Field names and constraints are
transcribed verbatim from the spec; the only intentional additions are
documented inline (``ge=0`` on MM007's minute budget).

Tool-name mapping: the class name IS the tool name exposed to the LLM
(e.g. ``RootCauseAnalysisTrace``), so providers see one tool per mental
model and the enforcement gate can map tool -> contract in O(1).

Contents
--------
MM001 ``OODAExecutionTrace`` · MM002 ``FirstPrinciplesDeconstruction`` ·
MM003 ``SixHatsEvaluation`` · MM004 ``ParetoOptimizationPlan`` ·
MM005 ``RootCauseAnalysisTrace`` · MM006 ``OccamHypothesisRanking`` ·
MM007 ``EmotionalRegulationFilter`` · MM008 ``EvolutionaryArchitecturePlan`` ·
MM009 ``InversionPreMortem`` · MM010 ``LegacyRefactoringAudit`` ·
MM011 ``ConsequenceMappingTree`` · MM012 ``SociotechnicalAlignmentCheck`` ·
MM013 ``ComplexityDomainClassifier`` · MM014 ``MetricGamingPrevention`` ·
MM015 ``TelemetryDriftValidation`` · ``SCHEMA_BY_MODEL_ID`` index.

Invariants
----------
* Class names double as LLM tool names (Strategy 1 of
  ``docs/ARCHITECTURE.md``) — never rename a class without updating
  ``SCHEMA_BY_MODEL_ID`` and the registry data modules' ``schema_class``
  values in lockstep.
* Model ids MM001–MM015 are stable identifiers used in audit records,
  the routing matrix (``registry/rule_data.py``) and validation-rule
  ``applies_to`` lists — never rename.
* Flow: node_4 (generation) emits traces that node_5 validates against
  these classes plus the owning model's VR* rules (VR016 is universal).
  MM013 doubles as the node_1 domain-classifier contract.

Cross-references
----------------
* Base class: :mod:`case_engine.schemas.base`
* Spec-verbatim data: ``case_engine/registry/model_data_a.py`` /
  ``model_data_b.py``; rules: ``case_engine/registry/rule_data.py``
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/VALIDATION_RULES.md``
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from case_engine.schemas.base import BaseTrace

__all__ = [
    "OODAExecutionTrace",
    "FirstPrinciplesDeconstruction",
    "SixHatsEvaluation",
    "ParetoOptimizationPlan",
    "RootCauseAnalysisTrace",
    "OccamHypothesisRanking",
    "EmotionalRegulationFilter",
    "EvolutionaryArchitecturePlan",
    "InversionPreMortem",
    "LegacyRefactoringAudit",
    "ConsequenceMappingTree",
    "SociotechnicalAlignmentCheck",
    "ComplexityDomainClassifier",
    "MetricGamingPrevention",
    "TelemetryDriftValidation",
    "SCHEMA_BY_MODEL_ID",
]


class OODAExecutionTrace(BaseTrace):
    """MM001 — OODA Loop execution trace (Observe → Orient → Decide → Act).

    Emitted by node_4 when MM001 ("OODA Loop") executes; validated at
    node_5 by VR001 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM001"``.

    Attributes:
        problem_statement: The problem under analysis.
        observe_raw_data: Unfiltered facts and telemetry.
        orient_context: Analysis of biases and context; VR001 requires it
            to reference at least one item from ``observe_raw_data``.
        decide_hypothesis: Chosen action/hypothesis.
        act_execution_plan: Steps to execute and loop back.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    observe_raw_data: list[str] = Field(..., description="Unfiltered facts and telemetry")
    orient_context: str = Field(..., description="Analysis of biases and context")
    decide_hypothesis: str = Field(..., description="Chosen action/hypothesis")
    act_execution_plan: str = Field(..., description="Steps to execute and loop back")


class FirstPrinciplesDeconstruction(BaseTrace):
    """MM002 — First Principles deconstruction trace.

    Emitted by node_4 when MM002 ("First Principles Thinking") executes;
    validated at node_5 by VR002 (+ universal VR016).  The class name
    doubles as the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM002"``.

    Attributes:
        problem_statement: The problem under analysis.
        current_assumptions: Legacy beliefs to challenge.
        fundamental_truths: Indisputable constraints; VR002 forbids
            analogy markers here.
        reconstructed_solution: Solution rebuilt from truths only.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    current_assumptions: list[str] = Field(..., description="Legacy beliefs to challenge")
    fundamental_truths: list[str] = Field(..., description="Indisputable constraints")
    reconstructed_solution: str = Field(..., description="Solution from truths only")


class SixHatsEvaluation(BaseTrace):
    """MM003 — Six Thinking Hats evaluation trace.

    Emitted by node_4 when MM003 ("Six Thinking Hats") executes;
    validated at node_5 by VR003 (+ universal VR016).  The class name
    doubles as the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM003"``.

    Attributes:
        problem_statement: The problem under analysis.
        white_hat_data: Pure facts.
        red_hat_emotions: Gut feelings.
        black_hat_risks: Risks; VR003 caps count at green-hat count.
        yellow_hat_benefits: Benefits.
        green_hat_creativity: Alternatives.
        blue_hat_decision: Final decision.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    white_hat_data: str = Field(..., description="Pure facts")
    red_hat_emotions: str = Field(..., description="Gut feelings")
    black_hat_risks: list[str] = Field(..., description="Risks")
    yellow_hat_benefits: list[str] = Field(..., description="Benefits")
    green_hat_creativity: list[str] = Field(..., description="Alternatives")
    blue_hat_decision: str = Field(..., description="Final decision")


class ParetoOptimizationPlan(BaseTrace):
    """MM004 — Pareto 80/20 optimization plan trace.

    Emitted by node_4 when MM004 ("Pareto Principle") executes; validated
    at node_5 by VR004 (+ universal VR016).  The class name doubles as
    the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM004"``.

    Attributes:
        problem_statement: The problem under analysis.
        total_inputs_cataloged: All inputs considered.
        vital_few_20_percent: Inputs driving 80% of results.
        trivial_many_80_percent: Inputs driving 20% of results.
        optimization_action: Plan to double down / eliminate.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    total_inputs_cataloged: list[str] = Field(..., description="All inputs considered")
    vital_few_20_percent: list[str] = Field(..., description="Inputs driving 80% of results")
    trivial_many_80_percent: list[str] = Field(..., description="Inputs driving 20% of results")
    optimization_action: str = Field(..., description="Plan to double down / eliminate")


class RootCauseAnalysisTrace(BaseTrace):
    """MM005 — Root Cause Analysis (5 Whys) trace.

    Emitted by node_4 when MM005 ("Root Cause Analysis (5 Whys)")
    executes; validated at node_5 by VR005 (+ universal VR016).  The
    class name doubles as the LLM tool name (Strategy 1); spec data
    lives in ``registry/model_data_a.py`` under ``algorithm_id="MM005"``.

    Attributes:
        problem_statement: The problem under analysis.
        why_1 … why_5: The five sequential why answers.
        validated_root_cause: Systemic flaw (VR005 forbids human blame).
        systemic_countermeasure: Process/code change preventing recurrence.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    why_1: str = Field(..., description="First 'why' answer (symptom layer)")
    why_2: str = Field(..., description="Second 'why' answer")
    why_3: str = Field(..., description="Third 'why' answer")
    why_4: str = Field(..., description="Fourth 'why' answer")
    why_5: str = Field(..., description="Fifth 'why' answer (systemic layer)")
    validated_root_cause: str = Field(..., description="Systemic flaw, not human error")
    systemic_countermeasure: str = Field(
        ..., description="Process/code change to prevent recurrence"
    )


class OccamHypothesisRanking(BaseTrace):
    """MM006 — Occam's Razor hypothesis ranking trace.

    Emitted by node_4 when MM006 ("Occam's Razor") executes; validated at
    node_5 by VR006 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM006"``.

    Attributes:
        problem_statement: The problem under analysis.
        observed_phenomenon: What was actually observed.
        hypotheses: Each dict carries ``explanation``,
            ``unverified_assumptions_count`` and ``explanatory_power``.
        simplest_viable_answer: Explanation with fewest assumptions
            (VR006 enforces consistency with ``hypotheses``).
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    observed_phenomenon: str = Field(..., description="What was actually observed")
    hypotheses: list[dict[str, Any]] = Field(
        ...,
        description="Each: {explanation, unverified_assumptions_count, explanatory_power}",
    )
    simplest_viable_answer: str = Field(..., description="Explanation with fewest assumptions")


class EmotionalRegulationFilter(BaseTrace):
    """MM007 — 5x5 Rule emotional-regulation trace.

    Emitted by node_4 when MM007 ("5x5 Rule") executes; validated at
    node_5 by VR007 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM007"``.

    Attributes:
        problem_statement: The problem under analysis.
        stressor_event: The triggering event.
        five_year_impact_assessment: Will this matter in 5 years?
        matters_in_five_years: Boolean verdict.
        emotional_budget_allocated_minutes: Minute budget; schema caps at
            5 (spec) — VR007 re-checks the conditional invariant.
        reframing_strategy: How to reframe and move on.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    stressor_event: str = Field(..., description="The triggering event")
    five_year_impact_assessment: str = Field(
        ..., description="Assessment of whether this matters on a 5-year horizon"
    )
    matters_in_five_years: bool = Field(
        ..., description="True when the stressor has real 5-year impact"
    )
    emotional_budget_allocated_minutes: int = Field(
        ..., ge=0, le=5, description="Minutes allotted to the stressor (<=5 by VR007)"
    )
    reframing_strategy: str = Field(..., description="How to reframe and move on")


class EvolutionaryArchitecturePlan(BaseTrace):
    """MM008 — Gall's Law evolutionary architecture plan.

    Emitted by node_4 when MM008 ("Gall's Law") executes; validated at
    node_5 by VR008 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_a.py`` under ``algorithm_id="MM008"``.

    Attributes:
        problem_statement: The problem under analysis.
        ultimate_complex_goal: Where the system must eventually land.
        simple_v1_working_system: Minimal v1; VR008 requires <=3 components.
        anticipated_bottlenecks: Where complexity will need to be added.
        evolutionary_path: Ordered complexity additions.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    ultimate_complex_goal: str = Field(
        ..., description="The complex end-state the system must eventually reach"
    )
    simple_v1_working_system: str = Field(..., description="Minimal system delivering core value")
    anticipated_bottlenecks: list[str] = Field(
        ..., description="Where complexity will need to be added later"
    )
    evolutionary_path: list[str] = Field(..., description="Step-by-step complexity additions")


class InversionPreMortem(BaseTrace):
    """MM009 — Inversion pre-mortem trace.

    Emitted by node_4 when MM009 ("Inversion") executes; validated at
    node_5 by VR009 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_b.py`` under ``algorithm_id="MM009"``.

    Attributes:
        problem_statement: The problem under analysis.
        desired_success_outcome: The goal being inverted.
        inverted_failure_brainstorm: "How do we guarantee failure?" ideas.
        critical_failure_vectors: Distilled failure modes.
        preemptive_safeguards: Safeguards; VR009 requires a 1:1 mapping
            with ``critical_failure_vectors``.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    desired_success_outcome: str = Field(..., description="The success goal being inverted")
    inverted_failure_brainstorm: list[str] = Field(
        ..., description="'How do we guarantee failure?' brainstorm ideas"
    )
    critical_failure_vectors: list[str] = Field(
        ..., description="Distilled failure modes that matter most"
    )
    preemptive_safeguards: list[str] = Field(
        ..., description="One safeguard per failure vector (VR009: 1:1 mapping)"
    )


class LegacyRefactoringAudit(BaseTrace):
    """MM010 — Chesterton's Fence legacy-refactoring audit.

    Emitted by node_4 when MM010 ("Chesterton's Fence") executes;
    validated at node_5 by VR010 (+ universal VR016).  The class name
    doubles as the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_b.py`` under ``algorithm_id="MM010"``.

    Attributes:
        problem_statement: The problem under analysis.
        legacy_component_description: What the legacy code does today.
        hypothesis_of_original_intent: Why it was probably built so.
        edge_cases_protected: Behaviour that must not regress.
        characterization_tests_required: Tests written BEFORE refactoring
            (VR010 requires at least one).
        safe_refactoring_plan: Ordered, test-guarded plan.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    legacy_component_description: str = Field(
        ..., description="What the legacy component does today"
    )
    hypothesis_of_original_intent: str = Field(
        ..., description="Best hypothesis for why it was probably built so"
    )
    edge_cases_protected: list[str] = Field(
        ..., description="Behaviour that must not regress during refactoring"
    )
    characterization_tests_required: list[str] = Field(
        ..., description="Tests written BEFORE refactoring (VR010: at least one)"
    )
    safe_refactoring_plan: str = Field(..., description="Ordered, test-guarded plan")


class ConsequenceMappingTree(BaseTrace):
    """MM011 — Second-Order Thinking consequence tree.

    Emitted by node_4 when MM011 ("Second-Order Thinking") executes;
    validated at node_5 by VR011 (+ universal VR016).  The class name
    doubles as the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_b.py`` under ``algorithm_id="MM011"``.

    Attributes:
        problem_statement: The problem under analysis.
        proposed_action: The action being evaluated.
        first/second/third_order_consequences: Tiered consequences; VR011
            requires >=2 items per tier.
        net_systemic_value: [-1, 1]-style judgement of net effect.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    proposed_action: str = Field(..., description="The action being evaluated")
    first_order_consequences: list[str] = Field(
        ..., description="Immediate consequences (VR011: >=2 items)"
    )
    second_order_consequences: list[str] = Field(
        ..., description="Second-order consequences (VR011: >=2 items)"
    )
    third_order_consequences: list[str] = Field(
        ..., description="Third-order consequences (VR011: >=2 items)"
    )
    net_systemic_value: float = Field(
        ..., description="[-1, 1]-style judgement of net systemic effect"
    )


class SociotechnicalAlignmentCheck(BaseTrace):
    """MM012 — Conway's Law sociotechnical alignment check.

    Emitted by node_4 when MM012 ("Conway's Law") executes; validated at
    node_5 by VR012 (+ universal VR016).  The class name doubles as the
    LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_b.py`` under ``algorithm_id="MM012"``.

    Attributes:
        problem_statement: The problem under analysis.
        desired_software_architecture: Target architecture.
        current_team_communication_structure: How teams talk today.
        identified_mismatches: Where org and architecture diverge.
        proposed_team_topology: New topology; VR012 requires bounded
            context / service boundary language.
        api_contract_requirements: Contracts that encode the boundary.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    desired_software_architecture: str = Field(
        ..., description="Target software architecture"
    )
    current_team_communication_structure: str = Field(
        ..., description="How teams communicate today"
    )
    identified_mismatches: list[str] = Field(
        ..., description="Where org structure and architecture diverge"
    )
    proposed_team_topology: str = Field(
        ..., description="New topology; VR012 requires bounded-context language"
    )
    api_contract_requirements: list[str] = Field(
        ..., description="API contracts that encode the team/service boundaries"
    )


class ComplexityDomainClassifier(BaseTrace):
    """MM013 — Cynefin complexity-domain classification (pipeline node_1).

    This is the only contract emitted at node_1: its ``assigned_domain``
    output drives the node_2 router.  Traces for this model are still
    generated at node_4 and validated at node_5 by VR013 (+ universal
    VR016).  The class name doubles as the LLM tool name (Strategy 1);
    spec data lives in ``registry/model_data_b.py`` under
    ``algorithm_id="MM013"``.

    Attributes:
        problem_statement: The user's raw problem statement.
        cause_and_effect_predictability: How knowable cause/effect is.
        assigned_domain: The Cynefin domain — Literal-constrained so the
            router can never receive an invalid domain.
        selected_methodology: Methodology implied by the domain (VR013
            checks it against the canonical keyword mapping).
        justification: Why this domain was chosen.
    """

    problem_statement: str = Field(..., description="The user's raw problem statement")
    cause_and_effect_predictability: Literal[
        "HIGH", "MODERATE", "RETROSPECTIVE_ONLY", "NONE"
    ] = Field(
        ...,
        description="How knowable cause and effect are for this problem",
    )
    assigned_domain: Literal["Clear", "Complicated", "Complex", "Chaotic"] = Field(
        ...,
        description="Cynefin domain feeding the node_2 router (never invalid)",
    )
    selected_methodology: str = Field(
        ..., description="Methodology implied by the domain (VR013 lookup)"
    )
    justification: str = Field(..., description="Why this domain was chosen")


class MetricGamingPrevention(BaseTrace):
    """MM014 — Goodhart's Law metric-gaming prevention.

    Emitted by node_4 when MM014 ("Goodhart's Law") executes; validated
    at node_5 by VR014 (+ universal VR016).  The class name doubles as
    the LLM tool name (Strategy 1); spec data lives in
    ``registry/model_data_b.py`` under ``algorithm_id="MM014"``.

    Attributes:
        problem_statement: The problem under analysis.
        primary_metric_target: The metric being targeted.
        anticipated_gaming_vector: How the metric will be gamed.
        mandatory_counter_metric: The paired counter-metric (VR014 checks
            it opposes the gaming vector).
        alignment_formula: Combined scoring formula.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    primary_metric_target: str = Field(..., description="The metric being targeted")
    anticipated_gaming_vector: str = Field(
        ..., description="How the metric is likely to be gamed"
    )
    mandatory_counter_metric: str = Field(
        ..., description="Paired counter-metric (VR014: must oppose the gaming vector)"
    )
    alignment_formula: str = Field(
        ..., description="Combined primary/counter scoring formula"
    )


class TelemetryDriftValidation(BaseTrace):
    """MM015 — Map is Not the Territory telemetry-drift validation.

    Emitted by node_4 when MM015 ("The Map is Not the Territory")
    executes; validated at node_5 by VR015 (+ universal VR016).  The
    class name doubles as the LLM tool name (Strategy 1); spec data
    lives in ``registry/model_data_b.py`` under ``algorithm_id="MM015"``.

    Attributes:
        problem_statement: The problem under analysis.
        theoretical_model_or_schema: The model/map of reality.
        live_telemetry_source: Where ground truth streams from.
        expected_drift_vectors: How the map can drift from territory.
        observability_requirements: Signals to watch (VR015 requires
            coverage of every drift vector).
        drift_detection_threshold: [0,1] drift threshold.
    """

    problem_statement: str = Field(..., description="The problem under analysis")
    theoretical_model_or_schema: str = Field(
        ..., description="The model/map of reality under validation"
    )
    live_telemetry_source: str = Field(
        ..., description="Where ground-truth telemetry streams from"
    )
    expected_drift_vectors: list[str] = Field(
        ..., description="Ways the map can drift from the territory"
    )
    observability_requirements: list[str] = Field(
        ..., description="Signals to watch (VR015: cover every drift vector)"
    )
    drift_detection_threshold: float = Field(
        ..., ge=0.0, le=1.0, description="[0,1] drift threshold triggering validation"
    )


#: Canonical model-id -> contract-class mapping used by the enforcement
#: gate (:class:`case_engine.enforcement.SchemaGate`) and the tool factory
#: to resolve a registry entry's ``schema_class`` to the executable
#: contract in O(1).  Keys are the stable ids MM001–MM015; values are the
#: tool-contract classes above (class name == LLM tool name, Strategy 1).
SCHEMA_BY_MODEL_ID: dict[str, type[BaseTrace]] = {
    "MM001": OODAExecutionTrace,
    "MM002": FirstPrinciplesDeconstruction,
    "MM003": SixHatsEvaluation,
    "MM004": ParetoOptimizationPlan,
    "MM005": RootCauseAnalysisTrace,
    "MM006": OccamHypothesisRanking,
    "MM007": EmotionalRegulationFilter,
    "MM008": EvolutionaryArchitecturePlan,
    "MM009": InversionPreMortem,
    "MM010": LegacyRefactoringAudit,
    "MM011": ConsequenceMappingTree,
    "MM012": SociotechnicalAlignmentCheck,
    "MM013": ComplexityDomainClassifier,
    "MM014": MetricGamingPrevention,
    "MM015": TelemetryDriftValidation,
}
