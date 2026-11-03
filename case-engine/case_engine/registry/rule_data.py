"""Cognitive registry data — validation rules VR001–VR016 and routing matrix.

Purpose & role
--------------
Raw spec-transcribed data loaded by
:class:`case_engine.registry.models.CognitiveRegistry` (fail-fast
consistency checks run at load time).

``RULES`` entries mirror the spec's ``validation_rules_registry``; the
executable heuristics live in :mod:`case_engine.validation.rules` (keyed
by ``rule_id``).  ``applies_to`` is derived from each model's
``validation_rules`` list and is used for registry consistency checks.

``ROUTING_MATRIX`` is verbatim from the spec: keys are Cynefin domains or
specialist domains, values are ``algorithm_id`` lists ranked by affinity.

Contents
--------
* ``RULES`` — VR001–VR016 definitions (VR016 is the universal
  confidence rule, ``applies_to=["UNIVERSAL"]``).
* ``ROUTING_MATRIX`` — 4 Cynefin + 4 specialist domains → ranked ids.
* ``CYNEFIN_METHODOLOGY_KEYWORDS`` — engine extension consumed by the
  VR013 heuristic in ``case_engine/validation/rules.py`` (the spec's
  lookup-table hint approximated with keyword sets, because the spec
  does not enumerate canonical methodology strings).

Invariants
----------
* Rule ids VR001–VR016 are stable identifiers referenced by model
  ``validation_rules`` lists and written to audit records — never rename.
* ``ROUTING_MATRIX`` values are ordered: the node_2 router uses a stable
  sort, so this ordering is the tie-breaker for equal affinity scores —
  do not reorder without a spec update.
* Every domain key must match :class:`case_engine.schemas.pipeline.CynefinDomain`
  / ``SpecialistDomain`` values exactly.

Cross-references
----------------
* Docs: ``docs/VALIDATION_RULES.md``; consumers:
  ``case_engine/registry/models.py`` (loader + consistency checks),
  ``case_engine/validation/rules.py`` (executors),
  ``case_engine/registry/routing.py`` (router).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Validation rules VR001–VR016 (spec: validation_rules_registry)
#
# rule_id is a stable identifier — never rename.  The "type" discriminator
# (semantic/structural/mathematical/conditional/lookup) selects how node_5
# executes the rule; semantic rules may additionally be upgraded with an
# LLM-as-judge verdict via "auditor_prompt".
# ---------------------------------------------------------------------------
RULES: list[dict] = [
    {
        "rule_id": "VR001",
        "name": "ensure_orient_references_observe_data",
        "type": "semantic",
        "applies_to": ["MM001"],
        "description": "OODA orient_context must explicitly reference >=1 item from observe_raw_data",
        "auditor_prompt": (
            "Check if orient_context explicitly references at least one element "
            "from observe_raw_data. If it introduces entirely new facts, REJECT."
        ),
        "implementation_hint": (
            "Embedding similarity > 0.6 between orient_context and any "
            "observe_raw_data item"
        ),
    },
    {
        "rule_id": "VR002",
        "name": "ensure_fundamental_truths_do_not_contain_analogies",
        "type": "semantic",
        "applies_to": ["MM002"],
        "description": "First Principles fundamental_truths must not contain analogy markers",
        "auditor_prompt": (
            "Scan fundamental_truths for phrases: 'similar to', 'like X', "
            "'industry standard', 'as done by'. If found, REJECT."
        ),
        "implementation_hint": "Regex + classifier on analogy markers",
    },
    {
        "rule_id": "VR003",
        "name": "ensure_black_hat_does_not_override_green_hat",
        "type": "structural",
        "applies_to": ["MM003"],
        "description": "Six Hats: black_hat_risks length must not exceed green_hat_creativity length",
        "auditor_prompt": (
            "Count ideas in green_hat vs risks in black_hat. If risks > ideas, "
            "REJECT - caution is dominating creativity."
        ),
        "implementation_hint": "len(black_hat_risks) <= len(green_hat_creativity)",
    },
    {
        "rule_id": "VR004",
        "name": "ensure_vital_few_is_smaller_than_trivial_many",
        "type": "mathematical",
        "applies_to": ["MM004"],
        "description": "Pareto: vital_few count must be < trivial_many count",
        "implementation_hint": "len(vital_few_20_percent) < len(trivial_many_80_percent)",
    },
    {
        "rule_id": "VR005",
        "name": "ensure_root_cause_is_not_human_error",
        "type": "semantic",
        "applies_to": ["MM005"],
        "description": "5 Whys: validated_root_cause must describe a systemic flaw, not blame a person",
        "auditor_prompt": (
            "If validated_root_cause contains 'user error', 'engineer forgot', "
            "'operator mistake', REJECT. Must reference process, architecture, "
            "or tooling."
        ),
        "implementation_hint": "NER + classifier for human-blame phrases",
    },
    {
        "rule_id": "VR006",
        "name": "ensure_simplest_has_lowest_assumption_count",
        "type": "mathematical",
        "applies_to": ["MM006"],
        "description": "Occam: simplest_viable_answer must match the hypothesis with min(unverified_assumptions_count)",
        "implementation_hint": "argmin(h.unverified_assumptions_count for h in hypotheses)",
    },
    {
        "rule_id": "VR007",
        "name": "if_not_matters_in_5_years_minutes_must_be_lte_5",
        "type": "conditional",
        "applies_to": ["MM007"],
        "description": "5x5: if matters_in_five_years is False, emotional_budget <= 5",
        "implementation_hint": (
            "if not matters_in_five_years: assert emotional_budget_allocated_minutes <= 5"
        ),
    },
    {
        "rule_id": "VR008",
        "name": "ensure_v1_is_minimal",
        "type": "semantic",
        "applies_to": ["MM008"],
        "description": "Gall's Law: simple_v1_working_system must describe <=3 components",
        "auditor_prompt": (
            "Count distinct components/services in simple_v1_working_system. "
            "If >3, REJECT - not minimal."
        ),
        "implementation_hint": "Component-counting classifier",
    },
    {
        "rule_id": "VR009",
        "name": "ensure_safeguards_count_matches_vectors_count",
        "type": "mathematical",
        "applies_to": ["MM009"],
        "description": "Inversion: len(preemptive_safeguards) == len(critical_failure_vectors)",
        "implementation_hint": "len(preemptive_safeguards) == len(critical_failure_vectors)",
    },
    {
        "rule_id": "VR010",
        "name": "ensure_tests_written_before_refactor",
        "type": "structural",
        "applies_to": ["MM010"],
        "description": "Chesterton's Fence: characterization_tests_required must be non-empty",
        "implementation_hint": "len(characterization_tests_required) >= 1",
    },
    {
        "rule_id": "VR011",
        "name": "ensure_deep_consequences_evaluated",
        "type": "structural",
        "applies_to": ["MM011"],
        "description": "Second-Order: each consequence tier must have >=2 items",
        "implementation_hint": "all(len(tier) >= 2 for tier in [first, second, third])",
    },
    {
        "rule_id": "VR012",
        "name": "ensure_teams_map_to_bounded_contexts",
        "type": "semantic",
        "applies_to": ["MM012"],
        "description": "Conway's Law: proposed_team_topology must reference bounded contexts",
        "auditor_prompt": (
            "Verify proposed_team_topology explicitly maps teams to bounded "
            "contexts or service boundaries."
        ),
        "implementation_hint": "Keyword presence: 'bounded context', 'service boundary', 'domain'",
    },
    {
        "rule_id": "VR013",
        "name": "ensure_methodology_matches_domain",
        "type": "lookup",
        "applies_to": ["MM013"],
        "description": "Cynefin: selected_methodology must match the canonical mapping for assigned_domain",
        "implementation_hint": "lookup_table[assigned_domain] contains selected_methodology",
    },
    {
        "rule_id": "VR014",
        "name": "ensure_counter_metric_opposes_gaming_vector",
        "type": "semantic",
        "applies_to": ["MM014"],
        "description": "Goodhart: mandatory_counter_metric must directly counter anticipated_gaming_vector",
        "auditor_prompt": (
            "Verify that the counter-metric, when improved, would reduce the "
            "gaming behavior described."
        ),
        "implementation_hint": "LLM-as-judge with specific prompt",
    },
    {
        "rule_id": "VR015",
        "name": "ensure_observability_covers_drift_vectors",
        "type": "structural",
        "applies_to": ["MM015"],
        "description": "Map!=Territory: each drift_vector must have >=1 corresponding observability_requirement",
        "implementation_hint": "len(observability_requirements) >= len(expected_drift_vectors)",
    },
    {
        "rule_id": "VR016",
        "name": "ensure_confidence_above_threshold",
        "type": "mathematical",
        "applies_to": ["UNIVERSAL"],
        "description": "Universal: confidence score must meet model's confidence_threshold",
        "implementation_hint": "trace.confidence >= model.confidence_threshold",
    },
]

# ---------------------------------------------------------------------------
# Cynefin routing matrix (spec-verbatim)
#
# Keys: 4 Cynefin domains + 4 specialist domains.  Values: algorithm_id
# lists in spec priority order — the node_2 router's stable sort uses this
# ordering as the tie-breaker for equal affinity, so do NOT reorder.
# ---------------------------------------------------------------------------
ROUTING_MATRIX: dict[str, list[str]] = {
    "Clear": ["MM004", "MM006", "MM001"],
    "Complicated": ["MM002", "MM003", "MM005"],
    "Complex": ["MM008", "MM011", "MM015", "MM013"],
    "Chaotic": ["MM001", "MM007", "MM009"],
    "Legacy_Refactoring": ["MM010", "MM012", "MM005"],
    "AI_Alignment_Metrics": ["MM014", "MM009", "MM011"],
    "IoT_Telemetry_Diagnosis": ["MM015", "MM005", "MM001"],
    "Team_Org_Design": ["MM012", "MM003", "MM011"],
}

#: Canonical Cynefin methodology keywords used by the VR013 deterministic
#: lookup (see ``ensure_methodology_matches_domain`` in
#: ``case_engine/validation/rules.py``).  The spec's lookup-table hint is
#: approximated with keyword sets because the spec does not enumerate
#: canonical methodology strings.  Substrings, matched case-insensitively.
CYNEFIN_METHODOLOGY_KEYWORDS: dict[str, list[str]] = {
    "Clear": [
        "standard", "sop", "best practice", "checklist", "automation",
        "straightforward", "procedure", "known solution",
    ],
    "Complicated": [
        "analysis", "expert", "investigat", "assess", "optimiz", "optimis",
        "evaluate", "diagnos", "measure", "analyz", "analy",
    ],
    "Complex": [
        "probe", "experiment", "agile", "safe-to-fail", "iterat", "emergent",
        "cohort", "explor", "hypothesis",
    ],
    "Chaotic": [
        "act", "stabiliz", "stabilis", "contain", "immediate", "crisis",
        "triage", "urgent", "decisive",
    ],
}
