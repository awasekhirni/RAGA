# C.A.S.E. Architecture

This document explains *why* the engine is shaped the way it is, how the
modules map onto the spec's five enforcement strategies, and where this
implementation deliberately deviates from the spec (and why).

---

## 1. Design philosophy

### 1.1 Determinism is a topology property, not a prompting property

The spec's central claim: *"we have enforced compliance, which is encoded in
graph topology, not in prompts. The LLM has no agency to skip steps."*
Concretely, in this codebase:

- **Control flow** lives in `pipeline/state_machine.py` — a declared edge
  table (`EDGES`) plus a runner that raises `TopologyViolationError` if any
  node attempts an undeclared transition. Nodes are plain Python functions;
  no LLM call ever decides where the pipeline goes next.
- **Content** is the only thing the LLM produces, and only through
  schema-gated tool calls (`enforcement/gate.py`). A payload that fails the
  Pydantic contract is rejected *before* any node can read it.

### 1.2 Typed contracts over executed strings

The spec's Strategy 1 sketch builds Pydantic models by `exec()`-ing registry
schema strings. This implementation deliberately **never executes registry
strings**:

| | `exec(schema_string)` (spec sketch) | Typed classes (this engine) |
|---|---|---|
| Arbitrary-code surface | Yes (registry data becomes code) | None |
| Contract ↔ JSON-schema drift | Possible | Impossible (generated from the class) |
| Static analysis / IDE support | None | Full |
| Testability | Runtime only | Import-time |

The spec's `pydantic_schema` strings are still stored verbatim in the
registry (`registry/model_data_a.py`, `model_data_b.py`) for interop and
diffing against the source spec — marked as *reference only*.
`enforcement/tool_factory.py` resolves `schema_class` → the authoritative
class in `schemas/mental_models.py` and refuses to build a tool when they
disagree (`RegistryError`).

### 1.3 Fail-closed everywhere

- A rule that *crashes* fails its trace (`validation/rules.py: run_rule`).
- A ledger that *cannot append* aborts the session
  (`pipeline/context.py: EngineContext.audit`).
- Reflexion that *exhausts* escalates to a human with partial traces
  (`pipeline/nodes.py: node_8_human_escalation`).
- Unknown tool names are enforcement faults, not rejections to be retried
  (`enforcement/gate.py`).

### 1.4 Provider agnosticism

`llm/base.py` defines a one-method protocol (`generate_tool_call`). OpenAI,
Anthropic, Ollama and vLLM adapters translate around it; the deterministic
MockLLMClient makes the entire test suite and demo run with no network. A
custom gateway is ~10 lines (see `examples/custom_provider.py`).

---

## 2. Layer map

```
┌────────────────────────────────────────────────────────────────────┐
│ Facade      case_engine/engine.py  ·  cli.py                        │
├────────────────────────────────────────────────────────────────────┤
│ Pipeline    pipeline/nodes.py (8 nodes)                            │
│             pipeline/state_machine.py (topology guard)             │
│             pipeline/langgraph_adapter.py (optional LangGraph)     │
│             pipeline/context.py (EngineContext + fail-closed audit)│
├────────────────────────────────────────────────────────────────────┤
│ Services    enforcement/gate.py       validation/engine.py         │
│             enforcement/tool_factory.py validation/rules.py        │
│                                       validation/llm_judge.py      │
├────────────────────────────────────────────────────────────────────┤
│ Providers   llm/base.py · mock_client.py · openai_client.py        │
│             anthropic_client.py · ollama_client.py                 │
├────────────────────────────────────────────────────────────────────┤
│ Evidence    audit/base.py · jsonl_ledger.py · sqlite_ledger.py     │
│             memory_ledger.py                                       │
├────────────────────────────────────────────────────────────────────┤
│ Knowledge   registry/ (15 models, 16 rules, routing matrix)        │
│ Contracts   schemas/ (BaseTrace + 15 typed contracts + reports)    │
├────────────────────────────────────────────────────────────────────┤
│ Foundation  config.py · exceptions.py · logging_setup.py · version  │
└────────────────────────────────────────────────────────────────────┘
```

Dependency rule: arrows point downward only. The pipeline imports services,
services import knowledge/contracts, nothing imports the facade — so every
layer is testable in isolation.

---

## 3. Module responsibilities

| Module | Responsibility | Key exports |
|---|---|---|
| `engine.py` | Wire everything; run sessions; build results; audit-chain verification | `CASEEngine` |
| `cli.py` | Human entry point: run/demo/models/show/verify/config | `main` |
| `config.py` | Spec `global_guardrails` as validated model; env/file resolution | `GlobalGuardrails`, `EngineConfig` |
| `registry/` | The cognitive knowledge base + consistency checking + routing | `CognitiveRegistry`, `ModelRouter` |
| `schemas/` | The 15 executable contracts; state/result/report models | `SCHEMA_BY_MODEL_ID`, `CASEResult` |
| `enforcement/` | Strategy 1: gate every tool call; build provider tool specs | `SchemaGate`, `ToolFactory` |
| `validation/` | node_5 as a service: schema + 16 rules + judge veto | `ValidationEngine`, `run_rule` |
| `llm/` | Provider adapters behind one protocol | `create_llm_client` |
| `audit/` | Strategy 4 (hash-chained evidence), 3 backends | `create_ledger`, `AuditLedger` |
| `pipeline/` | Strategy 2: the 8-node deterministic execution topology | `CASEStateMachine`, `NODES`, `EDGES` |

---

## 4. Spec-strategy → implementation mapping

| Spec strategy | Where implemented | Notes |
|---|---|---|
| 1. Schema-Enforced Tool Use ("fill-in-the-blank") | `enforcement/gate.py`, `enforcement/tool_factory.py`, adapters' `tool_choice="required"` | Gate re-validates even provider-enforced calls (zero trust) |
| 2. State-Machine Topology | `pipeline/state_machine.py` (pure Python), `pipeline/langgraph_adapter.py` (optional LangGraph) | Same nodes/edges in both runners |
| 3. Reflexion / LLM-as-Judge | `pipeline/nodes.py: node_6_reflexion_loop`, `validation/llm_judge.py` | Judge is veto-only; heuristic always runs first |
| 4. AST/Regex Guardrails | Deterministic heuristics in `validation/rules.py` (regex, counts, thresholds) | Constrained decoding (Outlines) intentionally out of scope for the hosted-API path — see `docs/LLM_PROVIDERS.md` |
| 5. DPO on Cognitive Traces | Out of engine scope; the hash-chained ledger emits exactly the preferred/rejected trace pairs a DPO pipeline needs | `TRACE_REJECTED` vs `TRACE_VALIDATED` events are the labelled dataset |

---

## 5. Deviations from the spec (and why)

1. **Reflexion returns to the failing node.** The spec's node_6 always
   transitions back to node_4 — but regenerating *reasoning traces* because
   *classification* failed is incoherent. This engine records
   `state["reflexion_target"]` before entering reflexion; node_6 returns to
   node_1 or node_4 accordingly. The topology guard still constrains node_6
   to `{node_1, node_4, node_8}` (declared in `EDGES`).
2. **Typed contracts instead of `exec`.** See §1.2.
3. **VR012 keyword set extended** with the topology vocabulary the spec's own
   gold example uses (`stream-aligned`, `end-to-end`, `platform team`,
   `owning`); otherwise the spec's example would fail its own rule.
4. **VR013 lookup approximated with keyword families**
   (`CYNEFIN_METHODOLOGY_KEYWORDS`) because the spec does not enumerate
   canonical methodology strings.
5. **`timestamp` uses tz-aware `datetime.now(timezone.utc)`** — the spec's
   `datetime.utcnow()` is deprecated and produces naive timestamps.
6. **MM007's minute budget gains `ge=0`** (negative minutes are nonsense);
   the spec's `le=5` cap is preserved exactly.
7. **node_5 validates all retained traces** (including the node_1
   classifier trace), which is strictly stronger than the spec's per-round
   validation and keeps the ledger complete.

All deviations are additive safety or necessary coherence fixes; none weaken
enforcement.

---

## 6. Security posture

- **Zero-trust tool execution** — provider-side schema enforcement is never
  trusted; `SchemaGate` re-parses every payload (`extra="forbid"`, so
  hallucinated fields are rejected).
- **No `eval`/`exec`** anywhere in the engine (verified by grep; tests
  construct everything through typed classes).
- **PII handling** — `tee_required_for_pii=True` declares the deployment
  constraint; sessions that declare PII context should be routed to TEE
  execution upstream of the engine. The ledger stores payload *hashes* in
  `TRACE_GENERATED`/`TRACE_LEDGER_WRITE` events; full payloads appear only
  where operators choose to keep them.
- **Audit fail-closed** — an unavailable ledger aborts the run; the engine
  never continues unaudited.
- **Logging hygiene** — log records carry ids and previews (`query[:120]`),
  never full payloads; `JsonFormatter` ships structured extras for SIEM.

## 7. Performance notes

- Hot path per session: 1 classifier call + N model calls (N ≤ 3) + ≤ 3
  reflexion rounds. All validation is CPU-bound regex/counting (microseconds
  per rule; the 2000 ms budget exists to catch pathological inputs).
- `JSONLLedger` fsyncs per append (durable by default); switch
  `fsync=False` for throughput benchmarks.
- The `MockLLMClient` answers in microseconds, making the 144-test suite
  complete in well under a second.
