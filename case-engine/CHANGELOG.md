# Changelog

All notable changes to the C.A.S.E. Engine. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are semantic.

## [Unreleased]

### Changed
- English-only demo strings: the i18n seam doctests (`messages.py`) and the
  G18 catalog tests (`test_p2_gaps.py`) now exercise overrides with English
  placeholder text (the seam itself remains language-agnostic by design).

_Nothing else pending — the 2.4.0 P3 assurance round shipped everything in
flight.  (The context-engineering roadmap CE-01…CE-12 in
`docs/CONTEXT_ENGINEERING.md` remains planned, not started.)_

## [2.4.0] — 2026-11-03

P3 assurance & research closure: **all seven gaps (G22–G28) are closed** —
a Hypothesis property/fuzz suite over the trust boundary, a 31-payload
prompt-injection pack wired into CI, concurrency tests for threads and
processes, a benchmark harness publishing p50/p99 per node, trust-boundary
coverage lifted to 95 %+ (package total 95 %), recorded-cassette provider
contract tests, and a deterministic mutation-testing baseline.  See
`docs/GAP_ANALYSIS.md` for the per-gap evidence trail.

### Added — G22: property-based/fuzz testing of the gate and rules
- `tests/unit/test_property_gate.py`: Hypothesis strategies generating
  adversarial tool-call arguments (extra keys, wrong types, deep nesting,
  zero-width/bidi unicode abuse, extreme scalars) asserting the SchemaGate
  **never approves a non-conformant payload and never crashes** on
  arbitrary input; unknown tool names always raise `ToolCallRejectedError`;
  `canonical_json` round-trip/order-blindness/determinism properties and
  `compute_event_hash` stability/sensitivity properties.  Deterministic in
  CI (`derandomize` profile), skips gracefully without `hypothesis`
  (moved into the `dev` extra).

### Added — G23: adversarial prompt-injection pack
- `tests/adversarial/`: a curated corpus of **31 known-bad prompts** across
  8 categories (tool-name hallucination, spec leak, rule baiting, delimiter
  smuggling, roleplay jailbreak, encoding obfuscation, authority forgery,
  data exfiltration), a standalone replay runner
  (`python -m tests.adversarial.report`) and the regression suite
  `tests/integration/test_adversarial_injection.py` running node_4→node_5
  end-to-end: no crash, defined terminal state, intact audit chain, PII
  payloads flagged (G1), synthesis only from validated traces.  Malicious
  *responses* are simulated with a poisoned mock client (extra fields +
  smuggled directives, type chaos, hallucinated tools, refusals) proving
  the gate catches injected completions and reflexion recovers.

### Added — G24: concurrency tests
- `tests/integration/test_concurrency.py`: 8 threads × 4 sessions on one
  engine (unique session ids, chain verifies), threads sharing one JSONL
  ledger and one SQLite ledger, **4 processes × 5 appends on one JSONL
  file** (clean serialization under flock, zero lost/duplicated seq),
  corrupt-tail and mid-file corruption raising clear `LedgerError`s, and
  JSONL→SQLite chain re-import preserving payloads.

### Added — G25: performance benchmarks
- `bench/run_bench.py` (+ `bench/README.md`): publishes p50/p95/p99 **per
  pipeline node** (via the G11 metrics hooks), full-pipeline
  latency/throughput, enforcement-gate overhead (approve vs reject),
  `canonical_json`/`compute_event_hash` primitive cost, and per-backend
  ledger append cost (memory / JSONL buffered / JSONL fsync / SQLite).
  Two profiles (`smoke` for CI, `standard` for release notes); JSON and
  markdown output.  Regression-tested in
  `tests/integration/test_bench.py`.

### Changed — G26: coverage at the trust boundary
- Coverage (module total) raised **88 % → 95 %**; every audit and
  enforcement module now ≥ 99 % (`anchor`, `crypto`, `memory_ledger`,
  `sqlite_ledger`, `base`, `gate`, `tool_factory`, `logging_setup`,
  `routing`, `state_machine`, `validation/engine` at 100 %; `jsonl_ledger`
  99 %).  The four named thin files: `llm/openai_client.py` 99 %,
  `logging_setup.py` 100 %, `registry/routing.py` 100 %,
  `pipeline/nodes.py` 96 %.  New edge tests in
  `tests/unit/test_p3_coverage_gaps.py` pin documented fail-closed
  branches (flock loss, tail adoption after a failed append, rotation and
  anchor failures, corrupt segments, SDK-missing adapters, webhook
  delivery failure, pack/config error paths).

### Added — G27: recorded-cassette provider contract tests
- `tests/cassettes/*.json` pin the OpenAI Chat-Completions and Anthropic
  Messages wire envelopes (with `meta.pinned_sdk` + refresh policy) and
  drive the adapters through a stub client: parsed `ToolCallResult` must
  match the recorded projection; degradation variants (no tool call,
  non-JSON arguments) raise exactly the documented errors; and when the
  real SDKs are installed the cassettes are validated against
  `openai.types.chat.ChatCompletion` / `anthropic.types.Message` —
  SDK shape drift now fails CI, not the field.

### Added — G28: mutation testing of the rule logic
- `tools/mutation_run.py`: deterministic, dependency-free mutation runner
  (token-level `<`⇄`<=`, `>`⇄`>=`, `==`⇄`!=`, `and`⇄`or`, `True`⇄`False`
  flips over the validation rules + enforcement gate) with per-mutant
  test scoping, restore-or-die guarantees and chunked resumption.
  `[tool.mutmut]` configured in `pyproject.toml` for the canonical
  community toolchain.  Baseline: **mutation score 0.98** (98/100 killed)
  after hardening — the run exposed and fixed 14 unpinned boundaries now
  covered by `tests/unit/test_p3_mutation_pins.py`; the two surviving
  mutants are documented equivalent mutants (`docs/MUTATION_BASELINE.md`).
  Run once per release, not per PR.

### Fixed
- **G23 enforcement-fault audit gap**: failures that propagate out of
  `CASEEngine.run`/`run_async` (including `ToolCallRejectedError`
  enforcement faults) now close the session with a terminal
  `SESSION_FAILED` audit event — previously an unknown-tool fault could
  escape between the audit brackets unaudited.
- `.gitignore`/packaging: `mutants/`, `.mutmut-cache/`, `.hypothesis/`
  are excluded from VCS and release archives.

## [2.3.0] — 2026-11-03

P2 gap closure: **all eight developer-experience & ecosystem gaps
(G14–G21) are closed** — CI/lint/type gates, an external registry-pack
format, an optional serving layer with the container story, the
`doctor`/`init`/`export` CLI verbs, repo hygiene docs, an i18n message
seam, a documented 3.11+ floor decision and the adversarial tour
example.  Regression-tested (39 new tests; 281 total), lint-clean
(ruff) and type-clean (mypy, 0 errors across the package).  See
`docs/GAP_ANALYSIS.md` for the per-gap evidence trail.

### Added — G14: CI pipeline + lint/type gates
- `.github/workflows/ci.yml`: GitHub Actions matrix (Python 3.11/3.12/3.13)
  running compileall, `ruff check`, `mypy`, the test suite, doctests,
  the offline examples and a CLI smoke — fully hermetic, no secrets.
- `[tool.ruff]` / `[tool.mypy]` sections in `pyproject.toml` (curated,
  high-signal rule set; every suppression justified inline; stale
  `noqa` flagged via RUF100).  The 3.11/3.12/3.13 classifiers now
  declared.  `dev` extra gains `ruff` and `mypy`.
- Codebase brought to zero ruff findings (E9/F/W605/I/UP/RUF100) and
  zero mypy errors (`check_untyped_defs` on) — including real fixes:
  a missing `Any` import in `pipeline/context.py` and the test suite,
  correctly typed JSONL ledger handles (`TextIO | None`), a
  double-`Regex.search` in VR005 (now walrus), narrowed `raise` in the
  retry drivers, and `CASEState | dict` annotations on the engine's
  result/escalation projections.

### Added — G15: external JSON registry packs
- `case_engine/registry/pack.py`: the JSON pack contract
  (`schema_version` pinned to `SCHEMA_VERSION`, `models`, `rules`,
  `routing_matrix`; unknown top-level keys rejected), validated by the
  same frozen entry models and the same consistency checker as the
  code-defined registry.
- `CognitiveRegistry.load_pack(path, overlay=...)` (complete or
  id-merged overlay) and `.dump_pack(path)`; module-level
  `load_registry_pack` / `dump_registry_pack` /
  `PACK_TOP_LEVEL_KEYS` re-exported from `case_engine.registry`.
- New `RegistryPackError` (a `RegistryError` subclass) with the pack
  path in `context`; every failure logged at ERROR before raising
  (fail-closed).  Example: `examples/registry_pack_demo.py`.

### Added — G16: serving story (optional FastAPI + Docker)
- `case_engine/server.py` (`case_engine[server]` extra): `create_app()`
  with `GET /health`, `POST /sessions` (runs the **async** engine path,
  G5), `GET /sessions/{id}` (bounded in-process mirror) and
  `GET /ledger/verify` (on-demand tamper evidence).  FastAPI imports
  stay lazy; error mapping preserves fail-closed semantics
  (422 gate rejections, 503 provider unavailable, 500 CASEError).
  Request bodies typed (`extra=forbid`); logs carry query *length*
  only, never content.
- `Dockerfile` (python:3.12-slim, non-root, `/data` evidence volume,
  healthcheck) and `docker-compose.yml` (persistent `case-data`
  volume).

### Added — G17: CLI `doctor`, `init`, `export`
- `case-engine doctor`: registry self-check (id contiguity, routing
  domains), ledger hash-chain health (missing ledger = warn, tampering
  = fail) and provider status (`--ping` constructs the client without
  an API call); `--json` machine mode; exit 0/1.
- `case-engine init`: writes a schema-pure `config.json` template and
  proves it round-trips through the strict `EngineConfig.from_file`
  loader; refuses overwrite without `--force`.
- `case-engine export`: dumps the G12 result store to JSON or CSV
  (projected columns) to a file or stdout.

### Added — G18: repo hygiene
- `CONTRIBUTING.md` (setup, the verification gate, the six ground
  rules, PR checklist), `SECURITY.md` (supported versions, disclosure
  process, threat model, known caveats), license resolved to the
  actual dual text (Apache-2.0 OR Commercial; `LICENSE` file + SPDX in
  `pyproject.toml` + Apache classifier).

### Added — G19: i18n message-catalog seam
- `case_engine/messages.py`: `MessageCatalog` protocol +
  `DictMessageCatalog`, process-level `set_catalog` / `reset_catalog`,
  and `msg(key, default, **params)` which never raises (catalog
  failures log a WARNING and fall back to the engine's English text).
- Wired at two choke points: validation verdicts
  (`vr.<ID>.pass` / `vr.<ID>.fail`) in `run_rule` and the
  infrastructure-escalation response (`escalation.infra_failure`,
  parameterized).  Inert by default — byte-identical output with no
  catalog installed.

### Added — G21: adversarial tour example
- `examples/adversarial_tour.py`: five fully offline stages —
  hallucinated tool (hard fail-closed rejection), invalid arguments
  and missing tool call (reflexion recovery), persistent misbehavior
  (typed escalation), and payload tampering (detected by the hash
  chain).

### Changed
- Engine `__version__` → 2.3.0 (lockstep with `pyproject.toml`).
- `docs/EXTENDING.md`: new "Registry packs" section describing the
  JSON pack contract, overlay mode and the review workflow.
- `docs/GAP_ANALYSIS.md`: all P2 gaps marked ✅ CLOSED (v2.3.0) with
  per-gap implementation + test evidence; P2 header count updated.
- `README.md`: documentation map gains CONTRIBUTING/SECURITY; new
  "Serving" section (FastAPI + Docker); CLI command list updated.
- `docs/FILE_REFERENCE.md`: regenerated for the new modules
  (`messages.py`, `server.py`, `registry/pack.py`).

### Verification
- 281 tests pass (242 pre-existing + 39 in `tests/unit/test_p2_gaps.py`);
  doctests, all 8 examples and the CLI smoke (now incl. `doctor`,
  `init`, `export`) all green; `ruff check` and `mypy case_engine/`
  are clean — the 242-test v2.1.0+v2.2.0 contract is untouched.

## [2.2.0] — 2026-11-02

P1 gap closure: **all nine production-readiness gaps (G5–G13) are closed** —
async API, provider resilience, ledger lifecycle, observability, result
persistence, the escalation round trip, judge budget guard and token
accounting — regression-tested (65 new tests; 242 total) and documented.
See `docs/GAP_ANALYSIS.md` for the per-gap evidence trail. Also includes
the context-engineering audit & upgrade plan (`docs/CONTEXT_ENGINEERING.md`).

### Added — G5: async API (phase 1)
- `BaseLLMClient.generate_tool_call_async` — thread-offloaded default twin
  of the sync call; every adapter (mock/OpenAI/Anthropic/Ollama/vLLM/custom)
  is async-capable with identical semantics; the awaiting event loop is
  never blocked by a provider round-trip.
- `CASEStateMachine.run_async` — same `EDGES` topology guard, step bound,
  error semantics; nodes dispatched via `asyncio.to_thread`.
- `CASEEngine.run_async` — full mirror of `run` (audit bracketing, state
  seeding, escalation packaging, result-store persistence). Sync/async
  outcome parity and loop-responsiveness are regression-tested.

### Added — G6: escalation round trip (human-in-the-loop)
- `case_engine/escalation.py`: `EscalationHandoff` (typed, JSON-safe
  ticket), `EscalationSink` protocol, `FileEscalationSink` (fsync'd JSONL),
  `LoggingEscalationSink`, `WebhookEscalationSink` (stdlib HTTP POST),
  `handoff_from_result`, `EscalationDeliveryError`.
- node_8 delivers the handoff best-effort (a down ticketing system never
  fails a working escalation) and audits the outcome as `HOOK_FIRED`.
- `CASEEngine.resume(prior, reviewer_verdict, reviewer_id=..., approve=...)`:
  guided re-run carrying the human verdict as operator context *data*
  (never instructions), under the full constitution.
- `EngineConfig.escalation_sink_kind/path`; new example
  `examples/escalation_roundtrip.py`.

### Added — G7: token/cost accounting
- `ToolCallResult.usage` (normalized prompt/completion/total dict; OpenAI
  and Anthropic adapters extract provider usage), JSON-safe
  `state["llm_usage"]` accumulator, `AuditEventType.LLM_USAGE` event
  (counts only, emitted on success and escalation), and
  `CASEResult.token_usage` (`TokenUsage`).

### Added — G8: provider resilience (retry/backoff)
- `case_engine/llm/retry.py`: validated `RetryPolicy` (exponential backoff
  + jitter, honouring numeric `Retry-After`), transport-failure classifier
  (408/429/5xx/529 + timeout/connection), `call_with_retries` /
  `acall_with_retries`.
- OpenAI/Anthropic adapters retry transient transport failures
  (constructor: `retry_attempts=3` default, `1` disables); exhausted
  budgets normalize into `LLMProviderUnavailableError` at the existing
  boundary. Content failures stay reflexion-routed (never retried).

### Added — G9: JSONL cross-process safety
- `JSONLLedger(cross_process_safe=True)` (default): per-append advisory
  `flock` + tail re-read under the lock, adopting externally advanced
  chains instead of forking them; fcntl-less platforms degrade with a
  logged note (SQLite documented as the multi-writer choice).

### Added — G10: ledger lifecycle (rotation + anchoring + schema version)
- `JSONLLedger(max_bytes=...)`: size-based rotation into
  `<stem>.segN<suffix>` segments (genesis re-anchored per segment),
  `verify_all_segments()` / `iter_all_events()`.
- `case_engine/audit/anchor.py`: `AnchorSink` protocol,
  `FileAnchorSink`, `MemoryAnchorSink`, `make_anchor_record`;
  `JSONLLedger.anchor(note=...)` + auto-anchor on rotation.
- `AuditEvent.schema_version` (default 1) + `AUDIT_EVENT_SCHEMA_VERSION`;
  `SESSION_START` payloads now carry the version inside the hashed
  material. Legacy v2.1.0 ledgers replay and verify unchanged.

### Added — G11: observability hooks (metrics)
- `case_engine/observability.py`: `Metrics` protocol, `NoopMetrics`
  (default), `InMemoryMetrics`, `render_prometheus` (text exposition,
  dependency-free), fail-open `safe_incr`/`safe_observe`.
- Instrumented: per-event audit counters, per-node calls + latency,
  LLM latency/adapter failures, reflexion retries, escalations,
  result-store outcomes, resumes. Bounded-vocabulary labels only.

### Added — G12: session-result persistence + `history` CLI
- `case_engine/storage.py`: `ResultStore` protocol,
  `SQLiteResultStore` (thread-safe upsert; projected query columns),
  `InMemoryResultStore`, `create_result_store` factory.
- `EngineConfig.result_store_backend/path`; engine saves completed
  results (success and escalation) best-effort.
- New CLI command `case-engine history [--store PATH] [--since ISO]
  [--limit N] [--session ID] [--json]`; `run --result-store PATH`.

### Added — G13: judge budget guard
- `LLMBackedJudge.for_guardrails(guardrails)`: deadline derived from
  `schema_validation_timeout_ms // 2` (250 ms floor); cumulative
  `audit_calls` counter.

### Changed
- `EngineContext` gained `metrics` and `escalation_sink` (both injectable,
  safe defaults). `EngineConfig` gained `result_store_backend`,
  `result_store_path`, `escalation_sink_kind`, `escalation_sink_path`.
- Adapter transport failures now raise `LLMProviderUnavailableError` (a
  subclass of the previously-raised `LLMAdapterError`) — callers catching
  `LLMAdapterError` are unaffected.
- `docs/CONTEXT_ENGINEERING.md` (planning): context-engineering audit and
  the CE-01…CE-12 upgrade roadmap (closes G7/G23 at the plan level).

### Verification
- 242 tests pass (177 pre-existing + 55 in `tests/unit/test_p1_gaps.py`
  + 10 in `tests/unit/test_async_api.py`); doctests, examples (incl. the
  new escalation round trip) and CLI smoke all green; the 177-test
  v2.1.0 contract is untouched.

## [2.1.0] — 2026-11-02

P0 gap closure: the four data-protection promises of the constitution are now
**enforced in code**, regression-tested and demonstrated. See
`docs/GAP_ANALYSIS.md` for the full audit trail (G1–G4 marked ✅ CLOSED).

### Added — G1: PII/TEE enforcement path
- `case_engine/privacy.py`: `PIIDetector` protocol, offline `RegexPIIDetector`
  (email, phone, SSN, Luhn-checked credit card, IPv4), `NoopPIIDetector`.
- node_1 PII gate (query + context scanned before any LLM call); new
  `"pii_escalation"` topology edge; `AuditEventType.TEE_DECISION`.
- `GlobalGuardrails.pii_policy` (`escalate` | `allow_with_flags`),
  `EngineConfig.tee_attested`, per-call `CASEEngine.run(tee_attested=...)`.
- `CASEState`/`CASEResult`: `pii_detected`, `pii_categories`, `tee_attested`,
  `tee_decision`, `pii_flagged`.

### Added — G2: fallback policies
- `fallback_behavior` expanded to four values: `escalate_to_human_with_partial_trace`
  (default), `abort_session`, `passthrough_with_flags`, `raise`.
- node_6 applies the policy at budget exhaustion; new `"passthrough"` edge to
  node_7; `AuditEventType.FALLBACK_APPLIED`; result fields `aborted`, `passthrough`
  (passthrough always yields `all_valid=False` + loud warnings).

### Added — G3: judge hard deadline
- `LLMBackedJudge(timeout_ms=...)` (default 1000 ms): audit calls run under a
  daemon-thread deadline; overrun withholds the judge's veto (explicit fail-open
  verdict + WARNING) instead of stalling the session. Veto-only contract unchanged.

### Added — G4: audit payload protection at rest
- **Redaction:** `PIIRedactor` (deterministic `[EMAIL_1]`-style tokenization,
  in-memory map only) wired into `EngineContext.audit()`; enabled via
  `config.redact_pii_in_audit` (default off = byte-identical legacy behavior).
- **Encryption:** `case_engine/audit/crypto.py` — `AEADPayloadCipher` (AES-256-GCM,
  optional `crypto` extra), JSONL/SQLite `payload_cipher=` parameter,
  `config.ledger_encryption`, `CASE_ENGINE_LEDGER_KEY` env key. Encrypt-then-chain:
  `verify_chain()` stays key-free; wrong key / tampered ciphertext fail closed.

### Changed
- Version bump 2.0.0 → 2.1.0; `pyproject.toml` gains the `crypto` extra.
- `docs/GAP_ANALYSIS.md`: G1–G4 marked CLOSED with implementation + verification
  notes; `docs/AUDIT_LEDGER.md` and `docs/PIPELINE.md` updated for the new
  behaviors; new `examples/pii_protection.py`.

### Tests
- 44 new tests in `tests/unit/test_p0_gaps.py` (G1 gate ×9, G2 policies ×4,
  G3 deadline ×4, G4 redaction ×4, G4 encryption ×9, topology regression ×3);
  suite now 189 tests, all offline.

## [2.0.0] — 2026-11-02
- Initial production release: 8-node deterministic pipeline, 15 mental models,
  16 validation rules, zero-trust schema gate, provider-agnostic LLM layer
  (mock/openai/anthropic/ollama/vllm), SHA-256 hash-chained audit ledger
  (JSONL/SQLite/memory), LangGraph adapter, CLI, 145-test offline suite,
  10 documentation files (incl. generated FILE_REFERENCE + CONVENTIONS).
