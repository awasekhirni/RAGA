# Extending C.A.S.E.

Five extension seams, each guarded by the registry consistency checker so
incomplete wiring fails at import/registration time — never silently at
runtime.  Since v2.3.0 the constitution data itself is also externalizable
as a JSON pack (seam 7), so most tuning no longer requires forking Python.

---

## 1. Add a mental model

1. **Contract** — add a typed Pydantic class in
   `case_engine/schemas/mental_models.py` (inherit `BaseTrace`; set
   `extra="forbid"` semantics for free), and register it in
   `SCHEMA_BY_MODEL_ID`:

   ```python
   class PreMortemDeepDive(BaseTrace):
       """MM016 — my new model."""
       problem_statement: str
       failure_story: str
       mitigations: List[str]

   SCHEMA_BY_MODEL_ID["MM016"] = PreMortemDeepDive
   ```

2. **Registry entry** — append to `registry/model_data_b.py`:

   ```python
   {
       "algorithm_id": "MM016",
       "algorithm_name": "Pre-Mortem Deep Dive",
       "schema_class": "PreMortemDeepDive",
       "category": "...",
       "tags": [...],
       "description_for_llm": "one paragraph injected into the system prompt",
       "domain_affinity": {"Complex": 0.8, ...},
       "input_contract": {"required": [...], "optional": [...]},
       "output_contract": {"produces": [...], "downstream_models": [...]},
       "pydantic_schema": "class PreMortemDeepDive(BaseModel): ...  # reference only",
       "validation_rules": ["VR016"],          # + any dedicated rule
       "examples": [{...gold payload with confidence...}],
       "chain_with": [],
       "confidence_threshold": 0.75,
       "tee_compatible": True,
       "audit_hooks": ["trace_ledger_write"],
       "summary_fields": ["failure_story"],
   }
   ```

3. **Routing** — add `MM016` to the relevant domains in `ROUTING_MATRIX`
   (`registry/rule_data.py`).
4. **Tests** — add a gold example; the registry integrity tests
   (`test_registry.py`) and schema tests (`test_schemas.py`) pick it up
   automatically (contract resolution, example validation).
5. Optionally a **dedicated rule** (§2 below).

The registry refuses: unknown rule references, unknown affinity domains,
`schema_class` mismatches, routing entries pointing at missing ids.

## 2. Add a validation rule

1. **Definition** in `registry/rule_data.py: RULES`:

   ```python
   {
       "rule_id": "VR017",
       "name": "ensure_mitigations_cover_top_failures",
       "type": "structural",
       "applies_to": ["MM016"],
       "description": "one-line enforcement statement",
       "auditor_prompt": "optional judge prompt for semantic rules",
       "implementation_hint": "len(mitigations) >= 3",
   }
   ```

2. **Heuristic** in `validation/rules.py`:

   ```python
   def _vr017(trace, model_def, guardrails):
       mitigations = list(getattr(trace, "mitigations", None) or [])
       if len(mitigations) < 3:
           return False, f"VR017 failed: {len(mitigations)} mitigations < 3."
       return True, f"VR017 passed: {len(mitigations)} mitigations."

   VALIDATORS["VR017"] = _vr017
   ```

   Contract: pure function, no I/O, duck-typed trace, returns
   `(passed, message)`. A raised exception fails the trace (fail-closed).
3. **Wire** the rule id into the model's `validation_rules`.
4. **Tests** — add pass/fail cases to
   `tests/unit/test_validation_rules.py` following the existing pattern
   (CASES dict: valid + invalid factory per rule).

For **semantic** rules, include an `auditor_prompt`; the judge then gets a
veto whenever one is configured (`SEMANTIC_RULE_IDS` in
`validation/llm_judge.py` lists the upgradeable ids — add yours).

## 3. Add an LLM provider

Implement the one-method protocol and register the factory
(`llm/__init__.py`):

```python
from case_engine.llm.base import ToolCallRequest, ToolCallResult

class MyGateway:
    name = "gateway"

    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        response = my_gateway_call(
            system=request.system_prompt,
            user=request.user_prompt,
            tools=request.tool_specs,
            force_tool=True,
            max_tokens=request.max_tokens,
        )
        return ToolCallResult(tool_name=response.tool, arguments=response.args,
                              provider=self.name)
```

Requirements (see `docs/LLM_PROVIDERS.md`): force the tool, return raw
arguments (the gate validates), wrap failures in
`LLMAdapterError`/`LLMResponseError`. Add the key to
`EngineConfig.provider`'s Literal and to `create_llm_client`'s factory map.
Test with an injected stub client — no network (pattern:
`tests/unit/test_llm_adapters.py`).

## 4. Add an audit-ledger backend

Subclass `audit/base.py: AuditLedger` (implement `append`, `iter_events`,
`close`) and reuse the inherited `verify_chain()` and `count()`:

```python
class PostgresLedger(AuditLedger):
    def append(self, event_type, session_id, node, payload):
        seq = self._seq + 1
        timestamp = datetime.now(timezone.utc).isoformat()
        event_hash = compute_event_hash(self._last_hash, seq, timestamp,
                                        event_type, session_id, node, payload)
        ...  # INSERT; then update self._seq/self._last_hash and return the AuditEvent
```

Then add the backend key to `audit/__init__.py: create_ledger` and to
`EngineConfig.ledger_backend`. Keep appends **append-only** and use
`compute_event_hash` + `canonical_json` unchanged — the chain format is the
portable contract.

## 5. Alternative runner (LangGraph)

`pipeline/langgraph_adapter.py` already wraps the same nodes in a
LangGraph `StateGraph` with conditional edges derived from the same `EDGES`
table. `run_case_graph(ctx, state)` runs it. Install with
`pip install "case-engine[langgraph]"`. Any other runner (Temporal,
StepFlow, Airflow...) can reuse the node functions directly — they are pure
`(state, ctx) -> (next_node, state)` callables and `EDGES` is data.

## 6. Deployment checklist

- [ ] Set `allow_unvalidated_passthrough=False` (default) in production.
- [ ] Choose a persistent ledger (`jsonl` or `sqlite`) with backup policy.
- [ ] Ship `case_audit.*` to your SIEM; alert on `ESCALATED` spikes and on
      `SESSION_FAILED` (ledger/infrastructure failures).
- [ ] Configure a judge for semantic rules when a provider is reachable.
- [ ] Set per-model `confidence_threshold` to your domain's bar (VR016).
- [ ] Route PII sessions through a TEE (`tee_required_for_pii=True`).

---

## 7. Registry packs — external JSON constitution data (v2.3.0)

Instead of editing `model_data_a/b.py` / `rule_data.py`, ship your
constitution as a JSON pack and load it explicitly:

```python
from case_engine.registry import CognitiveRegistry

# Complete replacement: the pack IS the registry.
reg = CognitiveRegistry.load_pack("packs/org_pack.json")

# Overlay: merge tuned entries over the default MM001–MM015 / VR001–VR016.
reg = CognitiveRegistry.load_pack("packs/tuned_mm005.json", overlay=True)

# Bootstrap: dump the built-in registry as a starting point.
CognitiveRegistry.load_default().dump_pack("packs/org_pack.json")
```

Pack document contract (unknown top-level keys are rejected):

```json
{
  "schema_version": "2.0.0",
  "generated_by": "case-engine dump_registry_pack",
  "models":  [ { "algorithm_id": "MM001", "...": "full MentalModelDefinition" } ],
  "rules":   [ { "rule_id": "VR001", "...": "full ValidationRuleDefinition" } ],
  "routing_matrix": { "Clear": ["MM001"], "Complex": ["MM005"] }
}
```

Guarantees (identical to code-defined data — a pack can never weaken
the constitution):

- `schema_version` must equal `case_engine.version.SCHEMA_VERSION`;
  mismatches fail closed with `RegistryPackError` (a `RegistryError`
  subclass carrying `pack_path` in `context`).
- Entries pass the same frozen Pydantic models (`extra="forbid"` —
  typos fail fast with the offending field named).
- The loaded registry runs the same consistency checker (unknown rule
  references, unknown affinity domains, dangling routing ids all
  raise `RegistryError`).
- Round-trip property (regression-tested): `dump_pack` → `load_pack`
  yields an equivalent registry.

**Review workflow**: keep packs in version control, review them like
code (ids `MM###`/`VR###` are contract — never rename), and run
`examples/registry_pack_demo.py` as a smoke check after edits.
Running the full test suite against a pack-based engine works
unchanged: inject it via `CASEEngine(config=..., registry=reg)`.

---

## 8. Compatibility floor (decision record)

The engine requires **Python >= 3.11** (CI tests 3.11/3.12/3.13).
Back-porting to 3.10 was evaluated in the v2.3.0 gap analysis
(`docs/GAP_ANALYSIS.md` G20) and deliberately deferred: the codebase
uses PEP 604 unions and 3.11-era idioms throughout, so a back-port is
a real maintenance commitment.  It will be reconsidered when a
concrete adopter requires it.
