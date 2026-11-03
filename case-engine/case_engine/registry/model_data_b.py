"""Cognitive registry data — mental models MM009–MM015 (verbatim from spec).

Purpose & role
--------------
Raw registry entries transcribed verbatim from the C.A.S.E. spec; the
second half of ``MODELS_A + MODELS_B`` consumed by
:class:`case_engine.registry.models.CognitiveRegistry` (field semantics
are documented on
:class:`~case_engine.registry.models.MentalModelDefinition`, and
:mod:`case_engine.registry.model_data_a` documents the shared data
conventions).

* ``pydantic_schema`` is retained verbatim for reference / interop only;
  the **authoritative** executable contracts are the typed Pydantic
  classes in :mod:`case_engine.schemas.mental_models` (keyed by
  ``schema_class``).

Contents
--------
MM009 Inversion · MM010 Chesterton's Fence · MM011 Second-Order Thinking
· MM012 Conway's Law · MM013 Cynefin Framework · MM014 Goodhart's Law ·
MM015 The Map is Not the Territory.

Invariants
----------
* ``algorithm_id`` values MM009–MM015 are stable identifiers referenced
  by the routing matrix, validation rules and audit records — never
  rename.  MM013 is special: it is the node_1 domain-classifier contract
  (its ``chain_with: ["ROUTER"]`` marks the loop back into node_2).
* ``MODELS_A + MODELS_B`` must concatenate to MM001…MM015 in order.

Cross-references
----------------
* Sibling data: ``model_data_a.py`` (MM001–MM008), ``rule_data.py``.
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/EXTENDING.md``.
"""

from __future__ import annotations

MODELS_B: list[dict] = [
    # ---- MM009 · Inversion · schema_class: InversionPreMortem ----
    {
        "algorithm_id": "MM009",
        "algorithm_name": "Inversion",
        "schema_class": "InversionPreMortem",
        "version": "1.0.0",
        "category": "Strategic Decision Making & Risk Mitigation",
        "tags": ["risk", "premortem", "strategy"],
        "description_for_llm": (
            "Invert the goal: brainstorm how to guarantee failure, then build "
            "safeguards against each vector."
        ),
        "domain_affinity": {"Chaotic": 0.85, "Complex": 0.7, "Complicated": 0.6, "Clear": 0.3},
        "input_contract": {
            "required": ["problem_statement", "desired_success_outcome"],
            "optional": ["constraints"],
        },
        "output_contract": {
            "produces": ["failure_vectors", "preemptive_safeguards"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class InversionPreMortem(BaseModel):\n"
            "    problem_statement: str\n"
            "    desired_success_outcome: str\n"
            "    inverted_failure_brainstorm: List[str]\n"
            "    critical_failure_vectors: List[str]\n"
            "    preemptive_safeguards: List[str]\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR009", "VR016"],
        "examples": [
            {
                "problem_statement": "Launch new SaaS product",
                "desired_success_outcome": "1000 paying customers in 6 months",
                "inverted_failure_brainstorm": [
                    "No one knows we exist",
                    "Product breaks on day 1",
                    "Pricing too high",
                    "Support overwhelmed",
                ],
                "critical_failure_vectors": [
                    "Zero distribution",
                    "Unreliable infra",
                    "Wrong pricing",
                    "No support process",
                ],
                "preemptive_safeguards": [
                    "Pre-launch waitlist + content",
                    "Load test 10x expected",
                    "Beta pricing validation",
                    "Hire 2 support before launch",
                ],
                "confidence": 0.88,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.75,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["preemptive_safeguards"],
    },
    # ---- MM010 · Chesterton's Fence · schema_class: LegacyRefactoringAudit ----
    {
        "algorithm_id": "MM010",
        "algorithm_name": "Chesterton's Fence",
        "schema_class": "LegacyRefactoringAudit",
        "version": "1.0.0",
        "category": "Legacy Refactoring & System Modernization",
        "tags": ["legacy", "refactoring", "modernization"],
        "description_for_llm": (
            "Do not refactor legacy code until you understand why it was "
            "built that way. Write characterization tests first."
        ),
        "domain_affinity": {"Complicated": 0.8, "Complex": 0.6, "Clear": 0.3, "Chaotic": 0.1},
        "input_contract": {
            "required": ["problem_statement", "legacy_component_description"],
            "optional": ["git_blame", "docs"],
        },
        "output_contract": {
            "produces": ["hypothesis_of_intent", "characterization_tests", "safe_refactoring_plan"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class LegacyRefactoringAudit(BaseModel):\n"
            "    problem_statement: str\n"
            "    legacy_component_description: str\n"
            "    hypothesis_of_original_intent: str\n"
            "    edge_cases_protected: List[str]\n"
            "    characterization_tests_required: List[str]\n"
            "    safe_refactoring_plan: str\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR010", "VR016"],
        "examples": [
            {
                "problem_statement": "Refactor pricing module",
                "legacy_component_description": "1500-line function with nested if/else and magic numbers",
                "hypothesis_of_original_intent": "Handles 12 regional tax edge cases from 2015 compliance rules",
                "edge_cases_protected": [
                    "EU VAT reverse charge",
                    "California nexus threshold",
                    "Charity exemptions",
                ],
                "characterization_tests_required": [
                    "test_eu_vat_reverse_charge",
                    "test_ca_nexus_threshold",
                    "test_charity_exemption",
                ],
                "safe_refactoring_plan": (
                    "Write characterization tests → verify green → refactor "
                    "incrementally → keep tests green"
                ),
                "confidence": 0.82,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.8,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["safe_refactoring_plan"],
    },
    # ---- MM011 · Second-Order Thinking · schema_class: ConsequenceMappingTree ----
    {
        "algorithm_id": "MM011",
        "algorithm_name": "Second-Order Thinking",
        "schema_class": "ConsequenceMappingTree",
        "version": "1.0.0",
        "category": "Consequence Mapping & Strategy",
        "tags": ["strategy", "consequences", "long-term"],
        "description_for_llm": (
            "Ask 'And then what?' to map first, second, and third-order "
            "consequences of an action."
        ),
        "domain_affinity": {"Complex": 0.9, "Complicated": 0.7, "Clear": 0.3, "Chaotic": 0.4},
        "input_contract": {
            "required": ["problem_statement", "proposed_action"],
            "optional": ["time_horizon"],
        },
        "output_contract": {
            "produces": ["consequence_tree", "net_systemic_value"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class ConsequenceMappingTree(BaseModel):\n"
            "    problem_statement: str\n"
            "    proposed_action: str\n"
            "    first_order_consequences: List[str]\n"
            "    second_order_consequences: List[str]\n"
            "    third_order_consequences: List[str]\n"
            "    net_systemic_value: float\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR011", "VR016"],
        "examples": [
            {
                "problem_statement": "Force engineers to write 100% test coverage",
                "proposed_action": "Block PRs with <100% coverage",
                "first_order_consequences": ["Coverage jumps to 98%", "PR cycle time doubles"],
                "second_order_consequences": [
                    "Engineers write trivial tests to game metric",
                    "Real bugs still slip through",
                    "Morale drops",
                ],
                "third_order_consequences": [
                    "Top engineers leave",
                    "Test suite becomes maintenance burden",
                    "Coverage metric loses all meaning",
                ],
                "net_systemic_value": -0.7,
                "confidence": 0.9,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.75,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["second_order_consequences", "net_systemic_value"],
    },
    # ---- MM012 · Conway's Law · schema_class: SociotechnicalAlignmentCheck ----
    {
        "algorithm_id": "MM012",
        "algorithm_name": "Conway's Law",
        "schema_class": "SociotechnicalAlignmentCheck",
        "version": "1.0.0",
        "category": "Organizational Architecture & Team Topologies",
        "tags": ["org-design", "team-topology", "sociotechnical"],
        "description_for_llm": (
            "Software architecture mirrors org communication structure. "
            "Align teams to bounded contexts."
        ),
        "domain_affinity": {"Complex": 0.8, "Complicated": 0.7, "Clear": 0.3, "Chaotic": 0.2},
        "input_contract": {
            "required": [
                "problem_statement",
                "desired_software_architecture",
                "current_team_structure",
            ],
            "optional": [],
        },
        "output_contract": {
            "produces": ["mismatches", "proposed_team_topology", "api_contracts"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class SociotechnicalAlignmentCheck(BaseModel):\n"
            "    problem_statement: str\n"
            "    desired_software_architecture: str\n"
            "    current_team_communication_structure: str\n"
            "    identified_mismatches: List[str]\n"
            "    proposed_team_topology: str\n"
            "    api_contract_requirements: List[str]\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR012", "VR016"],
        "examples": [
            {
                "problem_statement": "Microservices too coupled",
                "desired_software_architecture": "5 independent microservices",
                "current_team_communication_structure": "1 monolithic team of 15 owning all services",
                "identified_mismatches": [
                    "Single team = implicit shared DB",
                    "No API boundaries enforced",
                    "Cross-service changes require full-team sync",
                ],
                "proposed_team_topology": (
                    "3 stream-aligned teams of 4-5, each owning 1-2 services "
                    "end-to-end + 1 platform team"
                ),
                "api_contract_requirements": [
                    "OpenAPI specs per service",
                    "Schema registry",
                    "Contract tests in CI",
                ],
                "confidence": 0.87,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.75,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["proposed_team_topology"],
    },
    # ---- MM013 · Cynefin Framework · schema_class: ComplexityDomainClassifier (node_1) ----
    {
        "algorithm_id": "MM013",
        "algorithm_name": "Cynefin Framework",
        "schema_class": "ComplexityDomainClassifier",
        "version": "1.0.0",
        "category": "Complexity Sense-making",
        "tags": ["sensemaking", "classification", "meta"],
        "description_for_llm": (
            "Classify the problem domain (Clear/Complicated/Complex/Chaotic) "
            "to select the right methodology."
        ),
        "domain_affinity": {"Clear": 0.5, "Complicated": 0.7, "Complex": 0.9, "Chaotic": 0.8},
        "input_contract": {
            "required": ["problem_statement"],
            "optional": ["context"],
        },
        "output_contract": {
            "produces": ["domain_classification", "selected_methodology"],
            "downstream_models": ["ROUTER"],
        },
        "pydantic_schema": (
            "class ComplexityDomainClassifier(BaseModel):\n"
            "    problem_statement: str\n"
            "    cause_and_effect_predictability: Literal['HIGH', 'MODERATE', 'RETROSPECTIVE_ONLY', 'NONE']\n"
            "    assigned_domain: Literal['Clear', 'Complicated', 'Complex', 'Chaotic']\n"
            "    selected_methodology: str\n"
            "    justification: str\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR013", "VR016"],
        "examples": [
            {
                "problem_statement": "Why are users churning?",
                "cause_and_effect_predictability": "RETROSPECTIVE_ONLY",
                "assigned_domain": "Complex",
                "selected_methodology": "Agile experiments + cohort probing",
                "justification": (
                    "Human behavior is only understandable in retrospect; must "
                    "probe with safe-to-fail experiments"
                ),
                "confidence": 0.88,
            }
        ],
        "chain_with": ["ROUTER"],
        "confidence_threshold": 0.7,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["assigned_domain", "selected_methodology"],
    },
    # ---- MM014 · Goodhart's Law · schema_class: MetricGamingPrevention ----
    {
        "algorithm_id": "MM014",
        "algorithm_name": "Goodhart's Law",
        "schema_class": "MetricGamingPrevention",
        "version": "1.0.0",
        "category": "Metrics, AI Alignment & Organizational Behavior",
        "tags": ["metrics", "alignment", "incentives"],
        "description_for_llm": (
            "When a measure becomes a target, it ceases to be a good measure. "
            "Always pair primary metrics with counter-metrics."
        ),
        "domain_affinity": {"Complex": 0.85, "Complicated": 0.7, "Clear": 0.4, "Chaotic": 0.3},
        "input_contract": {
            "required": ["problem_statement", "primary_metric_target"],
            "optional": ["current_gaming_observed"],
        },
        "output_contract": {
            "produces": ["gaming_vector", "counter_metric", "alignment_formula"],
            "downstream_models": [],
        },
        "pydantic_schema": (
            "class MetricGamingPrevention(BaseModel):\n"
            "    problem_statement: str\n"
            "    primary_metric_target: str\n"
            "    anticipated_gaming_vector: str\n"
            "    mandatory_counter_metric: str\n"
            "    alignment_formula: str\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR014", "VR016"],
        "examples": [
            {
                "problem_statement": "Measure engineer productivity",
                "primary_metric_target": "PRs merged per week",
                "anticipated_gaming_vector": "Engineers split work into tiny PRs to inflate count",
                "mandatory_counter_metric": "Average PR size + post-merge bug rate",
                "alignment_formula": "score = PRs_merged * avg_PR_substance / (1 + post_merge_bugs)",
                "confidence": 0.9,
            }
        ],
        "chain_with": [],
        "confidence_threshold": 0.8,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write"],
        "summary_fields": ["mandatory_counter_metric"],
    },
    # ---- MM015 · The Map is Not the Territory · schema_class: TelemetryDriftValidation ----
    {
        "algorithm_id": "MM015",
        "algorithm_name": "The Map is Not the Territory",
        "schema_class": "TelemetryDriftValidation",
        "version": "1.0.0",
        "category": "Data Modeling, IoT & Abstraction Management",
        "tags": ["iot", "observability", "digital-twin"],
        "description_for_llm": (
            "The schema/model is a lossy compression of reality. Always "
            "validate against live telemetry."
        ),
        "domain_affinity": {"Complex": 0.9, "Complicated": 0.6, "Clear": 0.3, "Chaotic": 0.4},
        "input_contract": {
            "required": ["problem_statement", "theoretical_model", "live_telemetry_source"],
            "optional": [],
        },
        "output_contract": {
            "produces": ["drift_vectors", "observability_requirements", "drift_threshold"],
            "downstream_models": ["MM005"],
        },
        "pydantic_schema": (
            "class TelemetryDriftValidation(BaseModel):\n"
            "    problem_statement: str\n"
            "    theoretical_model_or_schema: str\n"
            "    live_telemetry_source: str\n"
            "    expected_drift_vectors: List[str]\n"
            "    observability_requirements: List[str]\n"
            "    drift_detection_threshold: float = Field(..., ge=0.0, le=1.0)\n"
            "    confidence: float = Field(..., ge=0.0, le=1.0)\n"
            "    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())"
        ),
        "validation_rules": ["VR015", "VR016"],
        "examples": [
            {
                "problem_statement": "Digital twin shows healthy, physical sensors fail",
                "theoretical_model_or_schema": "PostgreSQL sensor_state table",
                "live_telemetry_source": "MQTT raw payload stream",
                "expected_drift_vectors": [
                    "Sensor battery degradation",
                    "Radio interference",
                    "Physical damage not in schema",
                ],
                "observability_requirements": [
                    "Battery voltage metric",
                    "RSSI per sensor",
                    "Payload checksum failure rate",
                ],
                "drift_detection_threshold": 0.15,
                "confidence": 0.88,
            }
        ],
        "chain_with": ["MM005"],
        "confidence_threshold": 0.8,
        "tee_compatible": True,
        "audit_hooks": ["trace_ledger_write", "drift_alert_emit"],
        "summary_fields": ["observability_requirements"],
    },
]
