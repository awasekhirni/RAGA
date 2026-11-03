# Validation Rules (VR001–VR016)

All 16 rules ship as **deterministic heuristics** in
`case_engine/validation/rules.py` — the engine runs fully offline. The five
semantic rules additionally accept an **LLM-as-judge veto**
(`validation/llm_judge.py`): when a judge is configured, its verdict is
AND-ed with the heuristic. The judge can **only veto, never rescue** — a
deliberately fail-closed combination. Judge failures downgrade gracefully to
heuristic-only with a WARNING.

Execution order: schema parse first (invalid traces never reach rules);
then the model's declared rules in declaration order; any rule crash fails
the trace (never the pipeline).

---

## VR001 — ensure_orient_references_observe_data *(MM001, semantic)*
OODA's Orient must reference the Observe data.
**Heuristic:** plural-tolerant token overlap; an observe item counts as
referenced when it shares ≥1 content token with `orient_context`.
**Judge prompt (spec):** *"Check if orient_context explicitly references at
least one element from observe_raw_data. If it introduces entirely new
facts, REJECT."*
**Fail message:** `orient_context introduces entirely new facts and references none of the observe_raw_data items.`

## VR002 — ensure_fundamental_truths_do_not_contain_analogies *(MM002, semantic)*
First-Principles truths must be analogy-free.
**Heuristic:** regex for `similar to`, `like a/an/the`, `just like`,
`as done by`, `as used by`, `industry standard`, `akin to`, `same as`,
`reminds me of`, `typical of`, `basically a`.
**Fail:** lists the offending truths and matched markers.

## VR003 — ensure_black_hat_does_not_override_green_hat *(MM003, structural)*
`len(black_hat_risks) <= len(green_hat_creativity)` — caution must not
dominate creativity.

## VR004 — ensure_vital_few_is_smaller_than_trivial_many *(MM004, mathematical)*
`len(vital_few_20_percent) < len(trivial_many_80_percent)`.

## VR005 — ensure_root_cause_is_not_human_error *(MM005, semantic)*
Root causes must be systemic.
**Heuristic (two checks):**
1. no blame phrases (`user error`, `human error`, `operator error/mistake`,
   `engineer forgot`, `forgot to`, `pilot error`, `pebkac`, `idiot`,
   `careless`, `negligen*`, `lazy`, `not my fault`, `their fault`, `blame`);
2. ≥1 systemic keyword (`process`, `architecture`, `tooling`, `automation`,
   `monitoring`, `alerting`, `configuration`, `pipeline`, `procedure`,
   `design`, `infrastructure`, `policy`, `training`, `interface`,
   `guardrail`, `schema`, `deploy`, `test`, `observability`, ...).
**Judge prompt (spec):** *"If validated_root_cause contains 'user error',
'engineer forgot', 'operator mistake', REJECT. Must reference process,
architecture, or tooling."*

## VR006 — ensure_simplest_has_lowest_assumption_count *(MM006, mathematical)*
`simplest_viable_answer` must correspond to
`argmin(h.unverified_assumptions_count)`.
**Heuristic:** the chosen hypothesis must appear in (or share ≥2 content
tokens with) the answer; malformed hypothesis entries fail.

## VR007 — if_not_matters_in_5_years_minutes_must_be_lte_5 *(MM007, conditional)*
If `matters_in_five_years is False` →
`emotional_budget_allocated_minutes <= 5`. (The schema already caps at 5;
VR007 guards the conditional invariant for dynamic/looser schemas.)

## VR008 — ensure_v1_is_minimal *(MM008, semantic)*
Gall's Law: the v1 system describes ≤3 components.
**Heuristic:** count segments split on `, ; + and with plus / • ·` **and**
explicit numbered markers (`1.`, `2)`); the larger signal wins.
**Judge prompt (spec):** *"Count distinct components/services. If >3,
REJECT — not minimal."* — the judge upgrade is recommended for prose-heavy
answers.

## VR009 — ensure_safeguards_count_matches_vectors_count *(MM009, mathematical)*
`len(preemptive_safeguards) == len(critical_failure_vectors)` — 1:1
coverage.

## VR010 — ensure_tests_written_before_refactor *(MM010, structural)*
`len(characterization_tests_required) >= 1` — no refactoring before
understanding.

## VR011 — ensure_deep_consequences_evaluated *(MM011, structural)*
All three consequence tiers must contain ≥2 items each.

## VR012 — ensure_teams_map_to_bounded_contexts *(MM012, semantic)*
**Heuristic:** case-insensitive presence of any boundary marker in
`proposed_team_topology`: `bounded context`, `service boundary`, `domain`,
`end-to-end`, `stream-aligned`, `platform team`, `owning`, `owns`,
`team owns`. *(The last four extend the spec's keyword set — see
docs/ARCHITECTURE.md §5: the spec's own gold example uses that vocabulary.)*
**Judge prompt (spec):** *"Verify proposed_team_topology explicitly maps
teams to bounded contexts or service boundaries."*

## VR013 — ensure_methodology_matches_domain *(MM013, lookup)*
`selected_methodology` must match the canonical mapping for
`assigned_domain`. Implemented as keyword families
(`registry/rule_data.py: CYNEFIN_METHODOLOGY_KEYWORDS`):
- Clear: standard / SOP / best practice / checklist / automation / procedure
- Complicated: analysis / expert / investigate / assess / optimize / diagnose / measure
- Complex: probe / experiment / agile / safe-to-fail / iterate / emergent / cohort / explore / hypothesis
- Chaotic: act / stabilize / contain / immediate / crisis / triage / urgent / decisive

## VR014 — ensure_counter_metric_opposes_gaming_vector *(MM014, semantic)*
**Heuristic:** plural-tolerant content-token overlap between
`anticipated_gaming_vector` and `mandatory_counter_metric` (≥1 shared
concept). The spec itself marks this rule *"LLM-as-judge"* — **the judge
upgrade is the recommended production configuration here.**
**Judge prompt (spec):** *"Verify that the counter-metric, when improved,
would reduce the gaming behavior described."*

## VR015 — ensure_observability_covers_drift_vectors *(MM015, structural)*
`len(observability_requirements) >= len(expected_drift_vectors)`.

## VR016 — ensure_confidence_above_threshold *(UNIVERSAL, mathematical)*
`trace.confidence >= model.confidence_threshold` (fallback:
`guardrails.default_confidence_threshold` = 0.7). Registered on every model.

---

## Using the judge

```python
from case_engine import CASEEngine, EngineConfig
from case_engine.validation import LLMBackedJudge
from case_engine.llm import create_llm_client

judge = LLMBackedJudge(create_llm_client("openai", model="gpt-4o", api_key="sk-..."))
engine = CASEEngine(EngineConfig(provider="openai"), judge=judge)
```

Judge calls use the same enforcement machinery as everything else: the
auditor must return a schema-gated `ComplianceAuditVerdict` tool call
(`passed`, `rationale`, `cited_evidence`) — no free-text parsing anywhere.

## Adding a rule

See `docs/EXTENDING.md` §2: implement the heuristic, register it in
`VALIDATORS`, add the rule definition to `registry/rule_data.py`, and wire
its id into the relevant models' `validation_rules`. The registry
consistency checker and the test-suite pattern (pass + fail case per rule)
keep additions honest.
