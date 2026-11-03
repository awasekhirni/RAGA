# The C.A.S.E. Execution Pipeline

Node-by-node contract for the 8-node deterministic pipeline. Every node is a
pure function `(state, ctx) -> (next_node, new_state)`; the state machine
(`pipeline/state_machine.py`) validates each transition against the declared
edge table before the next node runs.

---

## Declared topology (node_1 → node_8)

```
node_1 classify ──success──► node_2 route ──success──► node_3 inject ──success──► node_4 generate
   │ │ failure                    │ failure               │ failure                 │ │
   │ └────────────► node_6 ◄──────┴───────────────────────┘                        │ │
   │                    │ ▲                              success │                 │ │
   │          retry left│ │ exhausted                           ▼                 │ │
   │                    ▼ │                                   node_5 validate ◄──┘ │ schema_violation
   │                 node_4 node_8 ◄──────────────── invalid ────┘                   │
   │                          │                                                      │
   └──(reflexion_target)─────►│                    all_valid │ exhausted             │
                                ▼                              ▼                       │
                              END                         node_7 audit+synthesize ─────┘
                                                              │
                                                            END
```

Edge table (source of truth: `pipeline/state_machine.py: EDGES`):

| Node | Edge | Destination |
|---|---|---|
| node_1_intent_classifier | success | node_2_model_router |
| node_1_intent_classifier | failure | node_6_reflexion_loop |
| node_1_intent_classifier | pii_escalation *(v2.1.0)* | node_8_human_escalation |
| node_2_model_router | success | node_3_schema_injection |
| node_2_model_router | failure | node_8_human_escalation |
| node_3_schema_injection | success | node_4_llm_generation |
| node_3_schema_injection | failure | node_8_human_escalation |
| node_4_llm_generation | success | node_5_ast_validator |
| node_4_llm_generation | schema_violation | node_6_reflexion_loop |
| node_5_ast_validator | all_valid | node_7_audit_and_synthesis |
| node_5_ast_validator | invalid | node_6_reflexion_loop |
| node_6_reflexion_loop | retry | node_4_llm_generation |
| node_6_reflexion_loop | retry_classify | node_1_intent_classifier |
| node_6_reflexion_loop | exhausted | node_8_human_escalation |
| node_6_reflexion_loop | passthrough *(v2.1.0)* | node_7_audit_and_synthesis |
| node_7_audit_and_synthesis | success | END |
| node_8_human_escalation | success | END |

`node_6` chooses between `retry` / `retry_classify` using
`state["reflexion_target"]`, which the failing node sets before entering
reflexion (see docs/ARCHITECTURE.md §5, deviation 1).

---

## State contract (`CASEState`)

Spec fields: `session_id`, `user_query`, `domain_classification`,
`selected_model_ids`, `reasoning_traces`, `validation_errors`, `retry_count`,
`all_valid`, `final_response`.

Engine extensions: `current_node`, `next_node`, `phase`, `context`,
`forced_model_ids`, `domain_source` (`classified` | `preset`), `tool_specs`,
`reflexion_target`, `error_detail`, `escalated`, `started_at`, `finished_at`.

`reasoning_traces` maps `model_id -> TraceRecord` (serialized): payload,
attempts and the final `TraceValidationReport`.

---

## Node contracts

### node_1 — classify_cynefin_domain
- **In:** `user_query` (or a preset `domain_classification`).
- **PII/TEE gate (v2.1.0, G1):** the `PIIDetector` scans query + context FIRST.
  PII without TEE attestation follows `guardrails.pii_policy`:
  `escalate` → straight to node_8 (nothing is generated); `allow_with_flags` →
  proceed, session flagged. Every sighting is audited as `TEE_DECISION`
  (categories only, never raw values).
- **LLM call:** single forced tool `ComplexityDomainClassifier` (MM013).
- **Enforcement:** gate re-validation **and** full validation (VR013, VR016)
  before acceptance.
- **Out:** `domain_classification`, `domain_source`, MM013 trace retained,
  `pii_detected` / `pii_categories` / `tee_decision`.
- **Audit:** `TEE_DECISION` (when PII detected), `DOMAIN_PRESET` (fast path) or
  `DOMAIN_CLASSIFIED`; `TRACE_REJECTED` / `VALIDATION_FAILED` on failure.
- **Failure →** node_6 (target = node_1); PII-policy refusal → node_8.

### node_2 — select_cognitive_models
- **In:** `domain_classification`, optional `forced_model_ids`.
- **Logic:** rank `routing_matrix[domain]` by `domain_affinity`, tie-break by
  spec-priority order; top-N (default 3). Operator overrides bypass ranking
  and are audited as such. **No LLM involved.**
- **Out:** `selected_model_ids`.
- **Audit:** `MODELS_SELECTED` (includes the affinity score table).
- **Failure →** node_8 (unknown domain cannot be routed).

### node_3 — inject_pydantic_schemas
- **In:** `selected_model_ids`.
- **Logic:** build an OpenAI-shaped tool spec per model from its typed
  contract (`ToolFactory`); `tool_choice="required"` semantics declared.
- **Out:** `tool_specs`.
- **Audit:** `SCHEMAS_INJECTED` (tool names).
- **Failure →** node_8 (unresolvable registry id — a deploy-time bug).

### node_4 — generate_reasoning_trace
- **In:** `tool_specs`, `user_query`, `context`, prior `validation_errors`
  (reflexion feedback).
- **LLM calls:** one **forced** tool call per selected model; the prompt
  carries the model's `description_for_llm` plus, on retries, the exact
  validation feedback to fix.
- **Enforcement:** every returned call goes through `SchemaGate` before
  being stored. Any adapter error or gate rejection aborts the whole round
  (all-or-nothing per round, mirroring the spec's round semantics).
- **Out:** `reasoning_traces` updated (attempts counted per model).
- **Audit:** `TRACE_GENERATED` per trace (when `audit_every_trace`);
  `TRACE_REJECTED` with the gate errors; `VALIDATION_FAILED` on adapter
  errors.
- **Failure →** node_6 (target = node_4).

### node_5 — validate_against_registry
- **In:** all retained traces (including the classifier's).
- **Logic:** per trace — Pydantic parse → rule battery
  (`model.validation_rules`, in declaration order) → confidence check
  (VR016). Rules never run on a schema-invalid trace.
- **Out:** per-trace `TraceValidationReport`; `all_valid`; per-model
  feedback strings for the next generation round.
- **Audit:** `TRACE_VALIDATED` or `VALIDATION_FAILED` (with failed rule ids)
  per trace.
- **Transition:** all_valid → node_7, else node_6 (target = node_4).

### node_6 — reject_and_retry (reflexion)
- **Logic:** increment `retry_count`; audit the feedback; if
  `retry_count <= max_reflexion_retries` return to the failing stage, else
  apply `guardrails.fallback_behavior` (v2.1.0, G2): default escalation to
  node_8, `abort_session` → node_8 closes as `aborted=True` (no escalation
  claimed), `passthrough_with_flags` → node_7 with the failed traces flagged
  (`passthrough=True`, `all_valid=False`, loud WARNING + `FALLBACK_APPLIED`
  event — never silent). At most `max_reflexion_retries` regeneration cycles
  run after the initial attempt (e.g. 3 → up to 4 generation rounds total).
- **Audit:** `REFLEXION_RETRY` (retry_count + full error map);
  `FALLBACK_APPLIED` on the non-default exhaustion policies.

### node_7 — record_trace_and_respond
- **Logic:** (1) per model, fire its declared `audit_hooks` —
  `trace_ledger_write` becomes a `TRACE_LEDGER_WRITE` event carrying the
  payload hash; other hooks become `HOOK_FIRED` events (deployment-specific
  metrics are wired here); (2) synthesize `final_response` from each
  model's `summary_fields` + mean confidence + audit chain tail.
- **Audit:** `TRACE_LEDGER_WRITE`, `HOOK_FIRED`, `SESSION_COMPLETED`.
- **Transition:** END.

### node_8 — escalate_with_context
- **Logic:** package `{user_query, domain, selected_models, retry_count,
  validation_errors, error_detail, partial_traces}` and write it as one
  `ESCALATED` event; set an escalation notice as `final_response`.
- **Audit:** `ESCALATED`. **Transition:** END.

---

## Audit event types (`AuditEventType`)

`SESSION_START`, `DOMAIN_PRESET`, `DOMAIN_CLASSIFIED`, `MODELS_SELECTED`,
`SCHEMAS_INJECTED`, `TRACE_GENERATED`, `TRACE_REJECTED`, `TRACE_VALIDATED`,
`VALIDATION_FAILED`, `REFLEXION_RETRY`, `TRACE_LEDGER_WRITE`, `HOOK_FIRED`,
`ESCALATED`, `SESSION_COMPLETED`, `SESSION_FAILED`.

These names are a stable contract for downstream SIEM/analytics (see
docs/AUDIT_LEDGER.md).

## guarantees summary

| Property | Enforced by |
|---|---|
| Every trace is schema-valid | node_1/node_4 gate + node_5 parse |
| No skipped phases | `EDGES` + `TopologyViolationError` |
| Bounded retries | node_6 counter + guardrail |
| Human accountability | node_8 escalation package |
| Complete evidence | ledger events at every node boundary |
| Bounded runtime | `max_steps=64` guard in the machine runner |
