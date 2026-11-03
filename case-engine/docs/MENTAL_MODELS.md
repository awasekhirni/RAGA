# Mental Models (MM001–MM015)

Every model below is registered in `case_engine/registry/` with a typed
Pydantic contract in `case_engine/schemas/mental_models.py`. The contract
class name **is** the LLM tool name. All contracts share `BaseTrace`
(`confidence: float ∈ [0,1]`, tz-aware UTC `timestamp`, `extra="forbid"`,
string stripping).

Routing affinities are declared per Cynefin domain (0–1). The router picks
the top-N for the classified domain; specialist domains
(`Legacy_Refactoring`, `AI_Alignment_Metrics`, `IoT_Telemetry_Diagnosis`,
`Team_Org_Design`) route by spec priority.

---

## MM001 — OODA Loop *(Strategic Agility & Crisis Response)*
**Contract `OODAExecutionTrace`** · Chaotic **0.95** · threshold 0.70 · rules VR001, VR016
- `observe_raw_data: List[str]` — unfiltered facts/telemetry
- `orient_context: str` — context & bias analysis (VR001: must reference ≥1 observe item)
- `decide_hypothesis: str` · `act_execution_plan: str`
- Chains with: MM005, MM007 · Hooks: `trace_ledger_write`, `metric_emit_latency`
- Gold: Zone-4 soil-sensor spikes → EMI from new irrigation motor.

## MM002 — First Principles Thinking *(Deep Innovation & Deconstruction)*
**`FirstPrinciplesDeconstruction`** · Complicated **0.80** · threshold 0.80 · rules VR002, VR016
- `current_assumptions: List[str]` · `fundamental_truths: List[str]` (VR002: no analogy markers)
- `reconstructed_solution: str`
- Chains with: MM008 · Gold: 10× cheaper IoT device via Wi-Fi+solar rebuild.

## MM003 — Six Thinking Hats *(Parallel Thinking & Team Alignment)*
**`SixHatsEvaluation`** · Complicated **0.85** · threshold 0.70 · rules VR003, VR016
- `white_hat_data` · `red_hat_emotions` · `black_hat_risks` · `yellow_hat_benefits` · `green_hat_creativity` · `blue_hat_decision`
- VR003: risks must not outnumber creative alternatives.

## MM004 — Pareto Principle *(Resource Optimization & Leverage)*
**`ParetoOptimizationPlan`** · Clear **0.90** · threshold 0.75 · rules VR004, VR016
- `total_inputs_cataloged` · `vital_few_20_percent` · `trivial_many_80_percent` (VR004: vital < trivial) · `optimization_action`

## MM005 — Root Cause Analysis / 5 Whys *(Diagnostics & Systemic Fixing)*
**`RootCauseAnalysisTrace`** · Complicated **0.90** · threshold 0.80 · rules VR005, VR016
- `why_1 … why_5` · `validated_root_cause` (VR005: systemic, never human blame)
- `systemic_countermeasure` · Chains with: MM010 · Hooks: `+ incident_ticket_link`
- Gold: stale dashboard → missing queue-depth alerting.

## MM006 — Occam's Razor *(Heuristic Simplification & Debugging)*
**`OccamHypothesisRanking`** · Clear **0.90** · threshold 0.70 · rules VR006, VR016
- `hypotheses: List[{explanation, unverified_assumptions_count, explanatory_power}]`
- `simplest_viable_answer` (VR006: must be the argmin-assumption hypothesis)

## MM007 — 5x5 Rule *(Emotional Regulation & Resilience)*
**`EmotionalRegulationFilter`** · Chaotic **0.80** · threshold 0.60 · rules VR007, VR016
- `stressor_event` · `five_year_impact_assessment` · `matters_in_five_years: bool`
- `emotional_budget_allocated_minutes: int ∈ [0,5]` (schema cap; VR007 re-checks the conditional)
- `reframing_strategy` · `tee_compatible: False` · Chains with: MM001

## MM008 — Gall's Law *(Systems Evolution & Architecture)*
**`EvolutionaryArchitecturePlan`** · Complex **0.95** · threshold 0.75 · rules VR008, VR016
- `ultimate_complex_goal` · `simple_v1_working_system` (VR008: ≤3 components)
- `anticipated_bottlenecks` · `evolutionary_path` · Chains with: MM012

## MM009 — Inversion *(Strategic Decision & Risk Mitigation)*
**`InversionPreMortem`** · Chaotic **0.85** · threshold 0.75 · rules VR009, VR016
- `inverted_failure_brainstorm` · `critical_failure_vectors` · `preemptive_safeguards`
- VR009: safeguards count must equal failure-vector count (1:1 coverage).

## MM010 — Chesterton's Fence *(Legacy Refactoring & Modernization)*
**`LegacyRefactoringAudit`** · Complicated **0.80** · threshold 0.80 · rules VR010, VR016
- `hypothesis_of_original_intent` · `edge_cases_protected`
- `characterization_tests_required` (VR010: ≥1) · `safe_refactoring_plan`

## MM011 — Second-Order Thinking *(Consequence Mapping & Strategy)*
**`ConsequenceMappingTree`** · Complex **0.90** · threshold 0.75 · rules VR011, VR016
- `first/second/third_order_consequences` (VR011: ≥2 items per tier)
- `net_systemic_value: float`
- Gold: 100%-coverage mandate → metric gaming → talent flight (net −0.7).

## MM012 — Conway's Law *(Organizational Architecture & Team Topologies)*
**`SociotechnicalAlignmentCheck`** · Complex **0.80** · threshold 0.75 · rules VR012, VR016
- `identified_mismatches` · `proposed_team_topology` (VR012: bounded-context language)
- `api_contract_requirements`

## MM013 — Cynefin Framework *(Complexity Sense-Making)*
**`ComplexityDomainClassifier`** · Complex **0.90** · threshold 0.70 · rules VR013, VR016
- `cause_and_effect_predictability: Literal[HIGH|MODERATE|RETROSPECTIVE_ONLY|NONE]`
- `assigned_domain: Literal[Clear|Complicated|Complex|Chaotic]` (Literal-constrained → the router can never receive an invalid domain)
- `selected_methodology` (VR013: must match the domain) · `justification`
- **Pipeline role:** this is node_1; `chain_with: ["ROUTER"]`.

## MM014 — Goodhart's Law *(Metrics, AI Alignment & Incentives)*
**`MetricGamingPrevention`** · Complex **0.85** · threshold 0.80 · rules VR014, VR016
- `anticipated_gaming_vector` · `mandatory_counter_metric` (VR014: must oppose the vector — judge upgrade recommended)
- `alignment_formula`
- Gold: PR-count metric → tiny-PR gaming → counter: PR size + post-merge bugs.

## MM015 — The Map is Not the Territory *(Data Modeling, IoT & Abstractions)*
**`TelemetryDriftValidation`** · Complex **0.90** · threshold 0.80 · rules VR015, VR016
- `theoretical_model_or_schema` · `live_telemetry_source`
- `expected_drift_vectors` · `observability_requirements` (VR015: coverage ≥ vectors)
- `drift_detection_threshold: float ∈ [0,1]` · Chains with: MM005 · Hooks: `+ drift_alert_emit`

---

## Custom models

See `docs/EXTENDING.md` — a new model needs: a contract class, a registry
entry (affinities, threshold, rules, example), and optionally a new rule.
The registry consistency checker refuses incomplete wiring at import time.
