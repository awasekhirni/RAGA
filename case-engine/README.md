# C.A.S.E. — Cognitive Architectural Scaffolding & Enforcement Engine

**Deterministic mental-model reasoning for any LLM.**
Version 2.0.0 · Python ≥ 3.11 · Pydantic ≥ 2.5 · License: Apache-2.0 / Commercial dual-license

> *"LLMs are terrible at following 10-step reasoning instructions, but they are
> exceptionally good at fulfilling JSON tool calls."*
> C.A.S.E. turns that asymmetry into an architecture: mental models become
> **mandatory, schema-gated tool calls** executed inside a **deterministic
> state machine** with a **cryptographic audit trail** — so agentic drift is
> eliminated by construction, not by prompting.

---

## Why C.A.S.E. exists

Agentic AI stacks suffer *agentic drift*: models skip reasoning steps, answer
before analysing, blame humans instead of finding systemic causes, and quietly
ignore the methodology you asked for. Prompt engineering cannot fix this — it
is probabilistic all the way down. C.A.S.E. replaces prompt hope with four
mechanical guarantees:

| # | Guarantee | Mechanism |
|---|-----------|-----------|
| 1 | No response without a **valid structured reasoning trace** | Schema-Gated Tool Invocation (Pydantic V2 gate, `tool_choice="required"`) |
| 2 | No **skipped or reordered** reasoning phases | 8-node deterministic state machine; the LLM has **zero** control-flow agency |
| 3 | No **methodologically invalid** content | 16 executable validation rules (VR001–VR016) + optional LLM-as-judge veto |
| 4 | No **unaccountable** behaviour | Append-only, SHA-256 hash-chained audit ledger (JSONL / SQLite / memory) |

If the model cannot produce a compliant trace after `max_reflexion_retries`
regeneration cycles with explicit validation feedback, the session is
**escalated to a human** with all partial traces preserved (fail-closed).

## What's in the box

- **15 mental models** (MM001–MM015): OODA Loop, First Principles, Six Thinking
  Hats, Pareto, 5 Whys, Occam's Razor, the 5x5 Rule, Gall's Law, Inversion,
  Chesterton's Fence, Second-Order Thinking, Conway's Law, Cynefin,
  Goodhart's Law, Map≠Territory — each with a typed Pydantic contract,
  domain affinities, gold examples and audit hooks.
- **16 validation rules** (VR001–VR016): deterministic heuristics for all of
  them (works 100% offline); semantic rules upgradeable with an LLM-as-judge
  that can veto but never rescue.
- **Cynefin router**: classifies the problem (node_1), then deterministically
  routes the top-N models by declared domain affinity (node_2).
- **5 provider adapters**: Mock (deterministic, offline), OpenAI, Anthropic,
  Ollama, vLLM — plus a protocol for your own gateway in ~10 lines.
- **3 ledger backends**: JSONL (grep-able), SQLite (SQL-queryable), Memory
  (tests) — all hash-chained with the same tamper-evidence contract.
- **CLI**: `run`, `demo`, `models`, `show`, `verify`, `config`.

## Quickstart

```bash
pip install -e .            # core (pydantic only)
pip install -e ".[all]"     # + openai, anthropic, langgraph extras
```

### Zero-code demo (fully offline)

```bash
case-engine demo                        # full pipeline, mock LLM, audited
case-engine demo --reflexion            # ...with a scripted failure + recovery
case-engine models                      # list MM001–MM015
case-engine show MM005                  # full registry entry for 5 Whys
case-engine verify                      # verify the audit hash chain
case-engine doctor                      # health: registry, ledger, provider
case-engine init                        # write a validated config.json template
case-engine run "..." --result-store r.sqlite3
case-engine history --store r.sqlite3   # list stored sessions (G12)
case-engine export --store r.sqlite3 --format csv --out sessions.csv
python examples/adversarial_tour.py     # the full adversarial tour (G21)
```

### Python API

```python
from case_engine import CASEEngine

engine = CASEEngine()                       # mock provider + ./case_audit.jsonl
result = engine.run("Zone 4 soil sensors report anomalous spikes")

print(result.final_response)                # synthesized, validated answer
print(result.domain_classification)         # "Complex"
print(result.selected_model_ids)            # ["MM008", "MM011", "MM015"]
assert engine.verify_audit_chain().valid    # tamper-evidence check
```

### A real provider

```python
from case_engine import CASEEngine, EngineConfig

engine = CASEEngine(EngineConfig(
    provider="openai",                 # or "anthropic" | "ollama" | "vllm"
    llm_kwargs={"model": "gpt-4o", "api_key": "sk-..."},
    ledger_backend="sqlite",           # SQL-queryable audit trail
))
result = engine.run("Why are users churning?")
```

### CLI against a real model

```bash
case-engine run "Should we migrate to Kubernetes?" \
    --provider anthropic --model claude-sonnet-4-20250514 \
    --ledger sqlite --ledger-path prod_audit.sqlite3
```

## Architecture at a glance

```
 user query
     │
     ▼
┌─────────────────────────────  CASEEngine  ──────────────────────────────┐
│                                                                         │
│  node_1 classify ──► node_2 route ──► node_3 inject ──► node_4 generate │
│      (Cynefin)       (affinity       (tool         (schema-gated       │
│                       ranking)        specs)        LLM tool calls)     │
│         │                                        │                      │
│         │ on_failure                              ▼                      │
│         └──────────► node_6 reflexion ◄──────── node_5 validate         │
│                         │    │            (Pydantic + VR001–VR016)     │
│              retries left│    │ exhausted                               │
│                         ▼    ▼                                         │
│                      node_4    node_8 escalate ──► END                  │
│                                                                         │
│  node_5 all_valid ──► node_7 audit + synthesize ──► END                 │
└─────────────────────────────────────────────────────────────────────────┘
     │                 │                    │
     ▼                 ▼                    ▼
 Enforcement Gate   Hash-Chained       15 mental-model
 (Pydantic V2,      Audit Ledger       Pydantic contracts
 zero-trust)        (JSONL/SQLite)     (MM001–MM015)
```

Every arrow is **code, not prompting**. The LLM only fills schemas; it can
never choose its own path. Full details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/PIPELINE.md`](docs/PIPELINE.md).

## Documentation map

| Document | Contents |
|----------|----------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Design philosophy, layer map, module responsibilities, deviations from the spec |
| [`docs/PIPELINE.md`](docs/PIPELINE.md) | Node-by-node contract: inputs, outputs, transitions, audit events, error modes |
| [`docs/MENTAL_MODELS.md`](docs/MENTAL_MODELS.md) | All 15 models: contracts, affinities, thresholds, examples |
| [`docs/VALIDATION_RULES.md`](docs/VALIDATION_RULES.md) | All 16 rules: heuristic implementation, failure messages, judge upgrades |
| [`docs/AUDIT_LEDGER.md`](docs/AUDIT_LEDGER.md) | Hash-chain format, backends, verification, SIEM integration |
| [`docs/LLM_PROVIDERS.md`](docs/LLM_PROVIDERS.md) | Adapter interface, tool-choice semantics per provider, writing your own |
| [`docs/EXTENDING.md`](docs/EXTENDING.md) | Add a mental model / rule / provider / ledger backend — step by step |
| [`docs/FILE_REFERENCE.md`](docs/FILE_REFERENCE.md) | **Every file, class and function**: roles, signatures, one-line docs, coverage stats (100% docstrings, 444 log sites) |
| [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) | Engineering standards: logging levels & log-safety, exception hierarchy, fail-closed semantics, docstring style |
| [`docs/GAP_ANALYSIS.md`](docs/GAP_ANALYSIS.md) | Evidence-based gap audit: P0 (G1–G4), P1 (G5–G13) and P2 (G14–G21) all closed with per-gap evidence; P3 remains with impact, fix, effort — plus verified non-gaps |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, the verification gate, ground rules (fail-closed, log-safety, deterministic control plane), PR checklist |
| [`SECURITY.md`](SECURITY.md) | Supported versions, disclosure process, threat model, known deployment caveats (G18) |
| [`docs/CONTEXT_ENGINEERING.md`](docs/CONTEXT_ENGINEERING.md) | Context-engineering audit & upgrade plan (CE-01…CE-12): few-shot injection, prompt fencing, orchestration context, token budgets, versioned prompts — the roadmap to a world-class meta-agent |

## Serving over HTTP (optional, G16)

The engine is a library and a CLI; an optional FastAPI layer
(`case_engine[server]`) adds a thin HTTP surface without making
FastAPI a core dependency:

```bash
pip install -e ".[server]"
uvicorn --factory case_engine.server:create_app --port 8000
# or: docker compose up --build
```

| Endpoint | Behavior |
|---|---|
| `GET /health` | Version, provider, registry size (cheap) |
| `POST /sessions` | Full pipeline via the **async** engine path (`{"query": "..."}`) |
| `GET /sessions/{id}` | Fetch a result from the bounded in-process mirror |
| `GET /ledger/verify` | On-demand audit hash-chain verification |

Fail-closed semantics are preserved over HTTP (422 gate rejections,
503 provider unavailable); interactive OpenAPI docs ship at `/docs`.
Logs carry query *length* only — never query content.

## Configuration (spec `global_guardrails`, verbatim defaults)

| Guardrail | Default | Meaning |
|-----------|---------|---------|
| `max_reasoning_tokens` | 4096 | Token ceiling per reasoning/tool-call request |
| `schema_validation_timeout_ms` | 2000 | Soft latency budget per trace validation |
| `max_reflexion_retries` | 3 | Reject-and-regenerate cycles after the initial attempt |
| `security_posture` | `zero_trust_tool_execution` | Every tool call re-validated server-side |
| `default_confidence_threshold` | 0.7 | Floor when a model declares none |
| `allow_unvalidated_passthrough` | **False** | Debug-only escape hatch (logs loud warnings) |
| `audit_every_trace` | True | Ledger-write every generated trace |
| `tee_required_for_pii` | True | **Enforced (v2.1.0)**: PII without TEE attestation follows `pii_policy` |
| `pii_policy` | `escalate` | PII w/o attestation: `escalate` (fail-closed) or `allow_with_flags` |
| `fallback_behavior` | `escalate_to_human_with_partial_trace` | Exhausted-reflexion policy; also `abort_session`, `passthrough_with_flags`, `raise` |

Config file via `CASE_ENGINE_CONFIG=/path/config.json` or `EngineConfig.load(path)`.

**Data-protection options (v2.1.0):** `redact_pii_in_audit=True` tokenizes PII
out of audit payloads before persistence; `ledger_encryption=True` envelope-
encrypts payloads at rest (AES-256-GCM, `pip install 'case-engine[crypto]'`, key
via `CASE_ENGINE_LEDGER_KEY`) while keeping `case-engine verify` key-free; TEE
attestation via `tee_attested` in config or per call (`engine.run(q,
tee_attested=True)`). See `docs/GAP_ANALYSIS.md` (G1–G4) and
`examples/pii_protection.py`.

**Production-readiness options (v2.2.0, closes all P1 gaps G5–G13):**

- **Async** — `await engine.run_async(query)`: non-blocking event loop,
  sync-twin semantics (FastAPI/LangGraph-friendly).
- **Resilience** — adapters retry transient transport failures (408/429/5xx/529,
  timeouts) with exponential backoff + jitter and honour `Retry-After`
  (`retry_attempts=1` disables).
- **Token accounting** — every result carries `token_usage` (calls, prompt,
  completion, total) and each session writes an `LLM_USAGE` audit event.
- **Ledger lifecycle** — `JSONLLedger(max_bytes=..., anchor_sink=...)`: size-based
  rotation into `.segN` segments with per-segment verification, plus external
  head-hash anchoring (`audit/anchor.py`); advisory flock makes JSONL appends
  cross-process safe; `AuditEvent.schema_version` added.
- **Observability** — `Metrics` protocol (`NoopMetrics` default; `InMemoryMetrics`
  auto-installed at `log_level="DEBUG"`); `render_prometheus(engine.metrics)`
  exports `case_engine_*` counters/timings in Prometheus text format.
- **Result persistence** — `EngineConfig(result_store_backend="sqlite")` or
  `run --result-store PATH`; query with `case-engine history [--since ISO]
  [--session ID] [--json]`.
- **Escalation round trip** — `escalation_sink_kind="file"|"logging"` (or inject
  `WebhookEscalationSink`) delivers typed `EscalationHandoff` tickets from node_8;
  `engine.resume(prior_result, reviewer_verdict, reviewer_id=...)` closes the
  human-in-the-loop loop as a fully-governed guided re-run
  (`examples/escalation_roundtrip.py`).
- **Judge budget** — `LLMBackedJudge.for_guardrails(guardrails)` derives the audit
  deadline from `schema_validation_timeout_ms // 2` (250 ms floor).

## The 15 mental models

| ID | Model | Best domain | Rules |
|----|-------|-------------|-------|
| MM001 | OODA Loop | Chaotic 0.95 | VR001, VR016 |
| MM002 | First Principles Thinking | Complicated 0.80 | VR002, VR016 |
| MM003 | Six Thinking Hats | Complicated 0.85 | VR003, VR016 |
| MM004 | Pareto Principle (80-20) | Clear 0.90 | VR004, VR016 |
| MM005 | Root Cause Analysis (5 Whys) | Complicated 0.90 | VR005, VR016 |
| MM006 | Occam's Razor | Clear 0.90 | VR006, VR016 |
| MM007 | 5x5 Rule | Chaotic 0.80 | VR007, VR016 |
| MM008 | Gall's Law | Complex 0.95 | VR008, VR016 |
| MM009 | Inversion | Chaotic 0.85 | VR009, VR016 |
| MM010 | Chesterton's Fence | Complicated 0.80 | VR010, VR016 |
| MM011 | Second-Order Thinking | Complex 0.90 | VR011, VR016 |
| MM012 | Conway's Law | Complex 0.80 | VR012, VR016 |
| MM013 | Cynefin Framework | Complex 0.90 | VR013, VR016 |
| MM014 | Goodhart's Law | Complex 0.85 | VR014, VR016 |
| MM015 | The Map is Not the Territory | Complex 0.90 | VR015, VR016 |

## Testing

```bash
pip install -e ".[dev]"
pytest                      # 461 tests: units, all 16 rules, e2e, CLI, property, adversarial
pytest --cov=case_engine    # with coverage
```

The suite runs fully offline (deterministic MockLLM + in-memory ledgers) and
covers: registry integrity, schema strictness, every validation rule
(pass + fail), gate rejections, topology violations, reflexion recovery,
escalation packaging, ledger tampering detection, provider adapters
(stubbed, no network) and the CLI surface.

## Project layout

```
case_engine/
├── engine.py              # CASEEngine facade (run / verify / introspect)
├── cli.py                 # case-engine command line
├── config.py              # GlobalGuardrails + EngineConfig
├── exceptions.py          # CASEError hierarchy
├── logging_setup.py       # structured logging (console/JSON)
├── registry/              # 15 models + 16 rules + routing matrix (+ router)
├── schemas/               # BaseTrace, 15 contracts, pipeline state/results
├── enforcement/           # SchemaGate (Strategy 1) + ToolFactory
├── validation/            # 16 rules + LLMJudge + ValidationEngine (node_5)
├── llm/                   # base + mock + openai + anthropic + ollama/vllm
├── audit/                 # hash-chained ledgers: JSONL / SQLite / memory
└── pipeline/              # 8 nodes, state machine, LangGraph adapter
docs/                      # 10 deep-dive documents (incl. FILE_REFERENCE, CONVENTIONS, GAP_ANALYSIS)
examples/                  # 5 runnable examples (incl. pii_protection)
tests/                     # 461-test suite (unit + integration + adversarial)
```

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md) — v2.1.0 closes the four P0 data-protection
gaps (TEE/PII gate, fallback policies, judge deadline, audit redaction +
encryption at rest); **v2.2.0 closes all nine P1 production-readiness gaps
(G5–G13)**: async API, provider retry/backoff, JSONL flock + rotation +
external anchoring, metrics, result store + `history` CLI, escalation round
trip (`engine.resume`), judge budget guard and token accounting.

## License

Dual-licensed: Apache-2.0 for open-source use; commercial license available
for regulated-industry deployments (see `LICENSE`). Author: Syed Awase Khirni.
