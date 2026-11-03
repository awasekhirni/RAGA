"""Cognitive registry data — mental models MM001–MM008 (verbatim from spec).

Purpose & role
--------------
Raw registry entries transcribed verbatim from the C.A.S.E. spec; loaded
by :class:`case_engine.registry.models.CognitiveRegistry`, which parses
each dict into a frozen
:class:`~case_engine.registry.models.MentalModelDefinition` and runs
consistency checks at load time.

Each dict is a raw registry entry.  Field semantics are documented on
:class:`case_engine.registry.models.MentalModelDefinition`.

* ``pydantic_schema`` is retained verbatim from the spec for reference /
  interop.  The **authoritative** executable contracts are the typed
  Pydantic classes in :mod:`case_engine.schemas.mental_models` (keyed by
  ``schema_class``); the string form is never ``exec``'d by the engine.
* ``summary_fields`` lists the fields used by the synthesis node to build
  the user-facing response (engine extension, documented in
  ``docs/EXTENDING.md``).

Contents
--------
MM001 OODA Loop · MM002 First Principles Thinking · MM003 Six Thinking
Hats · MM004 Pareto Principle (80-20 Rule) · MM005 Root Cause Analysis
(5 Whys) · MM006 Occam's Razor · MM007 5x5 Rule · MM008 Gall's Law.

Invariants
----------
* ``algorithm_id`` values MM001–MM015 are stable identifiers referenced
  by the routing matrix, validation rules and audit records — never
  rename or change their meaning.
* ``MODELS_A + MODELS_B`` must concatenate to MM001…MM015 in order
  (registry integrity tests assert contiguous ids).
* Entries stay spec-verbatim: only the documented engine extensions
  (``tee_compatible``, ``audit_hooks``, ``summary_fields``) were added.

Cross-references
----------------
* Sibling data: ``model_data_b.py`` (MM009–MM015), ``rule_data.py``.
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/EXTENDING.md``,
  ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

MODELS_A: list[dict] = [
    # ---- MM001 · OODA Loop · schema_class: OODAExecutionTrace ----
    {
        "algorithm_id": "MM001",
        "algorithm_name": "OODA Loop",
        "schema_class": "OODAExecutionTrace",
        "version": "1.0.0",
        "category": "Strategic Agility & Crisis Response",
        "tags": ["crisis", "agile", "strategic", "competitive"],
        "description_for_llm": (
            "A continuous loop for rapid learning: Observe facts, Orient with "
            "context/biases, Decide on action, Act and re-loop."
        ),
        "domain_affinity": {"Chaotic": 0.95, "Complex": 0.6, "Complicated": 0.3, "Clear": 0.1},
        "input_contract": {
            "required": ["problem_statement", "live_telemetry_or_facts"],
            "optional": ["historical_context", "competitor_actions"],
        },
        "output_contract": {
            "produces": ["hypothesis", "execution_plan", "next_observe_targets"],
            "downstream_models": ["MM005", "MM007"],
        },
        "pydantic_schema": (
            "class OODAExecutionTrace(BaseModel):\n"
            "    problem_statement: str\n"
            "    observe_raw_data: List[str] = Field(..., description='Unfiltered facts and telemetry')\n"
            "    orient_context: str = Field(..., description='Analysis of biases and context')\n"
            "    decide_hypothesis: str = Field(..., description='Chosen action/hypothesis')\n"
            "    act_execution_plan: str = Field(..., description='Steps to execute and loop back')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR001", "VR016"],
        "examples": [
            {
                "problem_statement": "SycliQ Zone 4 soil sensors reporting anomalous spikes",
                "observe_raw_data": [
                    "Sensor A: 95% moisture (normal: 40%)",
                    "Sensor B: 92%",
                    "No rain in 7 days",
                    "New irrigation motor installed nearby",
                ],
                "orient_context": (
                    "Spike correlates with motor installation; likely EMI "
                    "interference, not actual moisture"
                ),
                "decide_hypothesis": "EMI from new motor is corrupting sensor readings",
                "act_execution_plan": (
                    "1) Shield sensor cables, 2) Add EMI filter, 3) Re-read "
                    "sensors, 4) Loop back to Observe"
                ),
                "confidence": 0.85,
            }
        ],
        "chain_with": ["MM005"],
        "confidence_threshold": 0.7,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write", "metric_emit_latency"],
        "summary_fields": ["decide_hypothesis", "act_execution_plan"],
    },
    # ---- MM002 · First Principles Thinking · schema_class: FirstPrinciplesDeconstruction ----
    {
        "algorithm_id": "MM002",
        "algorithm_name": "First Principles Thinking",
        "schema_class": "FirstPrinciplesDeconstruction",
        "version": "1.0.0",
        "category": "Deep Innovation & Problem Deconstruction",
        "tags": ["innovation", "architecture", "greenfield"],
        "description_for_llm": (
            "Deconstruct to foundational truths. Challenge all assumptions. "
            "Rebuild from scratch."
        ),
        "domain_affinity": {"Complicated": 0.8, "Complex": 0.7, "Clear": 0.2, "Chaotic": 0.1},
        "input_contract": {
            "required": ["problem_statement", "current_approach"],
            "optional": ["constraints"],
        },
        "output_contract": {
            "produces": ["fundamental_truths", "reconstructed_solution"],
            "downstream_models": ["MM008"],
        },
        "pydantic_schema": (
            "class FirstPrinciplesDeconstruction(BaseModel):\n"
            "    problem_statement: str\n"
            "    current_assumptions: List[str] = Field(..., description='Legacy beliefs to challenge')\n"
            "    fundamental_truths: List[str] = Field(..., description='Indisputable constraints')\n"
            "    reconstructed_solution: str = Field(..., description='Solution from truths only')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR002", "VR016"],
        "examples": [
            {
                "problem_statement": "Reduce IoT device cost 10x",
                "current_assumptions": ["Need cellular modem", "Need GPS", "Need 1-year battery"],
                "fundamental_truths": [
                    "Data needs: 1KB/hour",
                    "Coverage: farm has Wi-Fi",
                    "Power: solar available",
                ],
                "reconstructed_solution": "Wi-Fi + solar + 1KB/hour payload = $3 device vs $30 cellular",
                "confidence": 0.9,
            }
        ],
        "chain_with": ["MM008"],
        "confidence_threshold": 0.8,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["reconstructed_solution"],
    },
    # ---- MM003 · Six Thinking Hats · schema_class: SixHatsEvaluation ----
    {
        "algorithm_id": "MM003",
        "algorithm_name": "Six Thinking Hats",
        "schema_class": "SixHatsEvaluation",
        "version": "1.0.0",
        "category": "Parallel Thinking & Team Alignment",
        "tags": ["team", "brainstorm", "alignment"],
        "description_for_llm": (
            "Switch between 6 thinking modes sequentially: White (facts), Red "
            "(emotion), Black (risks), Yellow (benefits), Green (creativity), "
            "Blue (decision)."
        ),
        "domain_affinity": {"Complicated": 0.85, "Complex": 0.6, "Clear": 0.4, "Chaotic": 0.2},
        "input_contract": {
            "required": ["problem_statement", "proposal"],
            "optional": ["stakeholder_list"],
        },
        "output_contract": {
            "produces": ["multi_perspective_evaluation", "final_decision"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class SixHatsEvaluation(BaseModel):\n"
            "    problem_statement: str\n"
            "    white_hat_data: str = Field(..., description='Pure facts')\n"
            "    red_hat_emotions: str = Field(..., description='Gut feelings')\n"
            "    black_hat_risks: List[str] = Field(..., description='Risks')\n"
            "    yellow_hat_benefits: List[str] = Field(..., description='Benefits')\n"
            "    green_hat_creativity: List[str] = Field(..., description='Alternatives')\n"
            "    blue_hat_decision: str = Field(..., description='Final decision')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR003", "VR016"],
        "examples": [
            {
                "problem_statement": "Migrate to Kubernetes",
                "white_hat_data": "Current: 12 VMs, 99.5% uptime",
                "red_hat_emotions": "Team anxious about learning curve",
                "black_hat_risks": ["6-month ramp-up", "Debugging complexity"],
                "yellow_hat_benefits": ["Auto-scaling", "Faster deploys"],
                "green_hat_creativity": ["Hybrid with serverless", "GitOps from day 1"],
                "blue_hat_decision": "Proceed with phased migration, start with non-critical workloads",
                "confidence": 0.8,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.7,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["blue_hat_decision"],
    },
    # ---- MM004 · Pareto Principle (80-20 Rule) · schema_class: ParetoOptimizationPlan ----
    {
        "algorithm_id": "MM004",
        "algorithm_name": "Pareto Principle (80-20 Rule)",
        "schema_class": "ParetoOptimizationPlan",
        "version": "1.0.0",
        "category": "Resource Optimization & Leverage",
        "tags": ["optimization", "prioritization", "efficiency"],
        "description_for_llm": (
            "Identify the 20% of inputs driving 80% of results. Double down on "
            "vital few; eliminate trivial many."
        ),
        "domain_affinity": {"Clear": 0.9, "Complicated": 0.6, "Complex": 0.3, "Chaotic": 0.1},
        "input_contract": {
            "required": ["problem_statement", "inputs_catalog", "outcome_metrics"],
            "optional": [],
        },
        "output_contract": {
            "produces": ["vital_few", "trivial_many", "optimization_action"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class ParetoOptimizationPlan(BaseModel):\n"
            "    problem_statement: str\n"
            "    total_inputs_cataloged: List[str]\n"
            "    vital_few_20_percent: List[str] = Field(..., description='Inputs driving 80% of results')\n"
            "    trivial_many_80_percent: List[str] = Field(..., description='Inputs driving 20% of results')\n"
            "    optimization_action: str = Field(..., description='Plan to double down / eliminate')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR004", "VR016"],
        "examples": [
            {
                "problem_statement": "Optimize DB performance",
                "total_inputs_cataloged": [
                    "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10",
                ],
                "vital_few_20_percent": ["q1", "q2"],
                "trivial_many_80_percent": ["q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10"],
                "optimization_action": "Index q1, rewrite q2; defer rest",
                "confidence": 0.9,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.75,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["optimization_action"],
    },
    # ---- MM005 · Root Cause Analysis (5 Whys) · schema_class: RootCauseAnalysisTrace ----
    {
        "algorithm_id": "MM005",
        "algorithm_name": "Root Cause Analysis (5 Whys)",
        "schema_class": "RootCauseAnalysisTrace",
        "version": "1.0.0",
        "category": "Deep Diagnostics & Systemic Fixing",
        "tags": ["diagnostics", "postmortem", "reliability"],
        "description_for_llm": (
            "Ask 'Why?' 5 times to reach the systemic root cause, not the "
            "symptom or human error."
        ),
        "domain_affinity": {"Complicated": 0.9, "Complex": 0.7, "Clear": 0.4, "Chaotic": 0.3},
        "input_contract": {
            "required": ["problem_statement", "symptoms"],
            "optional": ["logs", "recent_changes"],
        },
        "output_contract": {
            "produces": ["root_cause", "systemic_countermeasure"],
            "downstream_models": ["MM010"],
        },
        "pydantic_schema": (
            "class RootCauseAnalysisTrace(BaseModel):\n"
            "    problem_statement: str\n"
            "    why_1: str\n    why_2: str\n    why_3: str\n    why_4: str\n    why_5: str\n"
            "    validated_root_cause: str = Field(..., description='Systemic flaw, not human error')\n"
            "    systemic_countermeasure: str = Field(..., description='Process/code change to prevent recurrence')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR005", "VR016"],
        "examples": [
            {
                "problem_statement": "Dashboard shows stale data",
                "why_1": "Cache not invalidated",
                "why_2": "Invalidation event dropped",
                "why_3": "Queue consumer lagged",
                "why_4": "Consumer had no backpressure",
                "why_5": "No monitoring on queue depth",
                "validated_root_cause": "Missing queue-depth alerting allowed silent consumer lag",
                "systemic_countermeasure": (
                    "Add Prometheus metric + PagerDuty alert on queue depth > 1000"
                ),
                "confidence": 0.92,
            }
        ],
        "chain_with": ["MM010"],
        "confidence_threshold": 0.8,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write", "incident_ticket_link"],
        "summary_fields": ["validated_root_cause", "systemic_countermeasure"],
    },
    # ---- MM006 · Occam's Razor · schema_class: OccamHypothesisRanking ----
    {
        "algorithm_id": "MM006",
        "algorithm_name": "Occam's Razor",
        "schema_class": "OccamHypothesisRanking",
        "version": "1.0.0",
        "category": "Heuristic Simplification & Debugging",
        "tags": ["debugging", "triage", "simplification"],
        "description_for_llm": (
            "When multiple explanations exist, the one with fewest unverified "
            "assumptions is usually correct."
        ),
        "domain_affinity": {"Clear": 0.9, "Complicated": 0.7, "Complex": 0.4, "Chaotic": 0.2},
        "input_contract": {
            "required": ["problem_statement", "observed_phenomenon"],
            "optional": ["candidate_hypotheses"],
        },
        "output_contract": {
            "produces": ["ranked_hypotheses", "simplest_viable_answer"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class OccamHypothesisRanking(BaseModel):\n"
            "    problem_statement: str\n"
            "    observed_phenomenon: str\n"
            "    hypotheses: List[Dict[str, Any]] = Field(..., description='Each: {explanation, unverified_assumptions_count, explanatory_power}')\n"
            "    simplest_viable_answer: str = Field(..., description='Explanation with fewest assumptions')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR006", "VR016"],
        "examples": [
            {
                "problem_statement": "API returning 500",
                "observed_phenomenon": "500 on /users endpoint only",
                "hypotheses": [
                    {
                        "explanation": "DB connection pool exhausted",
                        "unverified_assumptions_count": 3,
                        "explanatory_power": 0.7,
                    },
                    {
                        "explanation": "Null pointer in user serializer",
                        "unverified_assumptions_count": 1,
                        "explanatory_power": 0.9,
                    },
                ],
                "simplest_viable_answer": "Null pointer in user serializer",
                "confidence": 0.85,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.7,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["simplest_viable_answer"],
    },
    # ---- MM007 · 5x5 Rule · schema_class: EmotionalRegulationFilter ----
    {
        "algorithm_id": "MM007",
        "algorithm_name": "5x5 Rule",
        "schema_class": "EmotionalRegulationFilter",
        "version": "1.0.0",
        "category": "Emotional Regulation & Resilience",
        "tags": ["emotional", "resilience", "perspective"],
        "description_for_llm": (
            "If it won't matter in 5 years, don't spend more than 5 minutes "
            "upset about it."
        ),
        "domain_affinity": {"Chaotic": 0.8, "Complex": 0.5, "Complicated": 0.3, "Clear": 0.2},
        "input_contract": {
            "required": ["problem_statement", "stressor_event"],
            "optional": [],
        },
        "output_contract": {
            "produces": ["perspective_reframe", "emotional_budget"],
            "downstream_models": ["MM001"],
        },
        "pydantic_schema": (
            "class EmotionalRegulationFilter(BaseModel):\n"
            "    problem_statement: str\n"
            "    stressor_event: str\n"
            "    five_year_impact_assessment: str\n"
            "    matters_in_five_years: bool\n"
            "    emotional_budget_allocated_minutes: int = Field(..., le=5)\n"
            "    reframing_strategy: str\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR007", "VR016"],
        "examples": [
            {
                "problem_statement": "Harsh client email",
                "stressor_event": "Client called deliverable 'unacceptable'",
                "five_year_impact_assessment": "One email in 5-year career arc",
                "matters_in_five_years": False,
                "emotional_budget_allocated_minutes": 5,
                "reframing_strategy": "5-min walk, then respond professionally",
                "confidence": 0.9,
            }
        ],
        "chain_with": ["MM001"],
        "confidence_threshold": 0.6,
        "tee_compatible": False,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["reframing_strategy"],
    },
    # ---- MM008 · Gall's Law · schema_class: EvolutionaryArchitecturePlan ----
    {
        "algorithm_id": "MM008",
        "algorithm_name": "Gall's Law",
        "schema_class": "EvolutionaryArchitecturePlan",
        "version": "1.0.0",
        "category": "Systems Evolution & Architecture",
        "tags": ["architecture", "evolution", "mvp"],
        "description_for_llm": (
            "Complex systems that work evolved from simple systems that "
            "worked. Start simple; add complexity only when bottlenecks are "
            "verified."
        ),
        "domain_affinity": {"Complex": 0.95, "Complicated": 0.5, "Clear": 0.2, "Chaotic": 0.1},
        "input_contract": {
            "required": ["problem_statement", "ultimate_complex_goal"],
            "optional": ["timeline", "team_size"],
        },
        "output_contract": {
            "produces": ["simple_v1", "evolutionary_path"],
            "downstream_models": ["MM012"],
        },
        "pydantic_schema": (
            "class EvolutionaryArchitecturePlan(BaseModel):\n"
            "    problem_statement: str\n"
            "    ultimate_complex_goal: str\n"
            "    simple_v1_working_system: str = Field(..., description='Minimal system delivering core value')\n"
            "    anticipated_bottlenecks: List[str]\n"
            "    evolutionary_path: List[str] = Field(..., description='Step-by-step complexity additions')\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR008", "VR016"],
        "examples": [
            {
                "problem_statement": "Build crop monitoring platform",
                "ultimate_complex_goal": "10k sensors, real-time analytics, ML yield prediction",
                "simple_v1_working_system": "Single Node.js service + PostgreSQL + 10 sensors",
                "anticipated_bottlenecks": ["DB writes at 1k sensors", "ML training latency"],
                "evolutionary_path": [
                    "1. Add TimescaleDB at 500 sensors",
                    "2. Shard by region at 2k",
                    "3. Add Kafka at 5k",
                    "4. Add ML pipeline at 8k",
                ],
                "confidence": 0.85,
            }
        ],
        "chain_with": ["MM012"],
        "confidence_threshold": 0.75,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["simple_v1_working_system"],
    },
]
