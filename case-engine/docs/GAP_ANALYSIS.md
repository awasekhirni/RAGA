# Gap Analysis & Missing Features

> **Status (v2.1.0):** the four P0 gaps (**G1–G4**) are **CLOSED** — see the
> "✅ CLOSED" blocks in each section for the implementing files and the
> verification evidence. **All P3 items closed v2.4.0** (P1 closed v2.2.0, P2 closed
> v2.3.0) — G1–G28 are done; only the CE-01…CE-12 context roadmap remains planned.
>
> **Companion plan:** [`CONTEXT_ENGINEERING.md`](CONTEXT_ENGINEERING.md) audits
> how LLM context is assembled (the input side of the same problem) and defines
> upgrades CE-01…CE-12. It closes **G7** (token accounting → CE-05) and
> **G23** (prompt-injection suite → CE-11); **G8** (retry backoff) remains
> open but is complemented by CE-04 (prior-attempt echo).

An evidence-based audit of what the C.A.S.E. engine **does not yet do** — measured
against (a) its own specification, (b) production-readiness for real deployments,
and (c) the ecosystem maturity of comparable agent frameworks. Every claim below
was verified against the current source (grep/read of `case_engine/`); no
speculative gaps are listed. The companion strengths list ("verified non-gaps")
at the end records what was checked and found **already implemented**, so this
document cannot be read as criticism by inertia.

**Priorities:** `P0` = constitution/spec violation or data-safety risk ·
`P1` = required for production deployment · `P2` = developer experience / ecosystem ·
`P3` = assurance hardening. **Effort:** `S` ≤ 1 day · `M` = 2–5 days · `L` > 1 week.

---

## Executive summary

| Priority | Open gaps | Themes |
|---|---|---|
| **P0** | ~~4~~ **0 — all closed in v2.1.0** | TEE/PII gate, fallback policies, judge deadline, redaction + encryption at rest |
| **P1** | ~~9~~ **0 — all closed in v2.2.0** | ~~Sync-only core, no provider resilience layer, ledger lifecycle, observability, escalation dead-end~~ → async API (phase 1), retry/backoff, flock + rotation + anchoring, metrics, result store + history CLI, escalation round trip, judge budget guard, token accounting |
| **P2** | ~~10~~ **0 — all closed in v2.3.0** | ~~No CI/lint/type gates, hardcoded registry packs, no server/container story~~ → CI matrix (3.11–3.13) + ruff/mypy gates, JSON registry packs, FastAPI + Docker serving, `doctor`/`init`/`export` CLI, repo hygiene docs, i18n seam, adversarial tour example (3.11 floor kept: G20 closed by decision) |
| **P3** | 7 | No fuzz/adversarial/concurrency/perf suites; thin coverage in 4 files |

**P0 closure (v2.1.0) at a glance:**

| Gap | Implementation | Tests |
|---|---|---|
| G1 TEE/PII gate | `case_engine/privacy.py` (detectors) · node_1 gate + `pii_escalation` edge · `TEE_DECISION` event · `guardrails.pii_policy`, `config.tee_attested`, `run(tee_attested=...)` | `tests/unit/test_p0_gaps.py::TestG1TeeGate` (6) + `TestPIIDetectors` (3) |
| G2 fallback policies | `guardrails.fallback_behavior` now 4-valued; node_6 applies `abort_session` / `passthrough_with_flags` via new edges + `FALLBACK_APPLIED` event | `TestG2FallbackPolicies` (4) |
| G3 judge deadline | `LLMBackedJudge(timeout_ms=...)` daemon-thread deadline → fail-open verdict (veto withheld) | `TestG3JudgeDeadline` (4) |
| G4a redaction | `PIIRedactor` deterministic tokenization wired into `EngineContext.audit()` via `config.redact_pii_in_audit` | `TestG4Redaction` (4) |
| G4b encryption | `case_engine/audit/crypto.py` AES-256-GCM envelopes; encrypt-then-chain; key-free `verify_chain`; `config.ledger_encryption` + `CASE_ENGINE_LEDGER_KEY` | `TestG4Encryption` (9) |

Top 3 recommendations if you do nothing else: ~~close the TEE/PII enforcement
path~~ *(done)*, **add the provider resilience layer (G8)** — the cheapest big
reliability win, **stand up CI + lint + type gates (G14)** before the codebase
grows further.

---

## P0 — Spec-compliance gaps (the engine's own promises, partially enforced)

### G1 · `tee_required_for_pii` guardrail has no enforcement path — ✅ CLOSED (v2.1.0)
**Was:** every mental model declared `tee_compatible`, and the guardrail existed in
`config.py` — but no node read the flag; no session was ever routed into or
flagged for a TEE; no audit event recorded a TEE decision.

**✅ Implementation:**
- `case_engine/privacy.py` — `PIIDetector` protocol, offline `RegexPIIDetector`
  (email / phone / SSN / Luhn-checked credit card / IPv4, deliberately
  conservative), `NoopPIIDetector`.
- `pipeline/nodes.py :: node_1_intent_classifier` — the G1 gate runs **first**
  (query + context, before classification and before any LLM call).
- `pipeline/state_machine.py :: EDGES` — new `"pii_escalation":
  "node_8_human_escalation"` edge: fail-closed refusal, no generation sees the PII.
- `schemas/pipeline.py` — `AuditEventType.TEE_DECISION`; `CASEState`/`CASEResult`
  gain `pii_detected` / `pii_categories` / `tee_attested` / `tee_decision`
  (`pii_flagged` on the result).
- Policy: `GlobalGuardrails.pii_policy` (`"escalate"` default |
  `"allow_with_flags"`); attestation: `EngineConfig.tee_attested` or per-call
  `CASEEngine.run(tee_attested=...)`; `node_8` renders a dedicated refusal
  message naming categories only.
- Every PII sighting is audited as `TEE_DECISION` with categories — never raw values.

**Verification:** `tests/unit/test_p0_gaps.py::TestG1TeeGate` (escalate, flags,
attested, disabled guardrail, clean-query regression, PII-in-context) +
`TestPIIDetectors`; end-to-end via `examples/pii_protection.py` and the CLI
(`case-engine run "Email sam@corp.com …" --json` → `tee_decision="escalated"`).

### G2 · `fallback_behavior` guardrail is defined but never read — ✅ CLOSED (v2.1.0)
**Was:** the guardrail was consulted only for infrastructure failures; node_6's
budget-exhaustion path hardcoded escalation.

**✅ Implementation:**
- `config.py :: GlobalGuardrails.fallback_behavior` — now a 4-valued Literal:
  `escalate_to_human_with_partial_trace` (default, unchanged) · `abort_session` ·
  `passthrough_with_flags` · `raise` (unchanged infra semantics).
- `pipeline/nodes.py :: node_6_reflexion_loop` — reads the guardrail at
  exhaustion: `abort_session` sets `abort_requested` and routes to node_8, which
  closes the session as `aborted=True` (no escalation claimed; audited as
  `SESSION_FAILED` with reason `aborted_per_fallback_policy`);
  `passthrough_with_flags` sets `passthrough_accepted`, emits a loud WARNING + a
  `FALLBACK_APPLIED` audit event and proceeds to node_7 over the new
  `"passthrough"` edge — node_7 flags the answer (`passthrough=True`,
  `all_valid=False`) so consumers can never mistake it for a validated one.
- `schemas/pipeline.py` — `AuditEventType.FALLBACK_APPLIED`; state/result fields
  `abort_requested` / `aborted` / `passthrough_accepted` / `passthrough`.

**Verification:** `TestG2FallbackPolicies` (default escalation unchanged,
abort closes without escalation, passthrough proceeds flagged with
`FALLBACK_APPLIED` present, invalid policy rejected at config time).

### G3 · `schema_validation_timeout_ms` is soft-only (WARNING, no cancellation) — ✅ CLOSED (v2.1.0)
**Was:** the only runtime use measured elapsed time and warned after the fact;
the optional LLM judge — the one network call inside validation — had no
deadline, so a hung endpoint stalled the session indefinitely.

**✅ Implementation (hard cancellation for the network path; soft WARNING kept
for the deterministic battery, which is sub-millisecond by construction):**
- `validation/llm_judge.py :: LLMBackedJudge` — new `timeout_ms` constructor
  parameter (default 1000 ms): every audit call runs in a daemon worker thread;
  on overrun the judge **withholds its veto** — an explicit fail-open
  `JudgeVerdict(passed=True, rationale="…deadline…veto withheld (fail-open)")`
  with a WARNING record. The deterministic verdict stands; the session never
  stalls; hung provider calls leak only daemon threads (never block interpreter
  exit — documented).
- Veto-only contract preserved: a deadline can only *remove* an adversarial
  review, never flip a heuristic rejection into an approval.

**Verification:** `TestG3JudgeDeadline` — hung client → fail-open verdict +
WARNING captured; direct call with `timeout_ms=None`/`0`; client errors still
degrade through the documented exception path.

### G4 · No PII redaction or encryption at rest in ledger payloads — ✅ CLOSED (v2.1.0)
**Was:** `grep encrypt|redact|mask case_engine/audit/` found nothing; any PII in
a task statement landed in plaintext in `case_audit.jsonl` / SQLite, making the
immutable evidence chain a GDPR/CCPA liability.

**✅ Implementation (both recommended measures):**
- **(a) Redaction hook (G4a)** — `case_engine/privacy.py :: PIIRedactor`:
  deterministic session-scoped tokenization (`jane@corp.com` → `[EMAIL_1]`;
  the map lives in memory only, never persisted). Wired into
  `pipeline/context.py :: EngineContext.audit()` — payloads are redacted
  **before** hashing, so the chain commits to exactly the persisted bytes.
  Enabled via `config.redact_pii_in_audit=True` (default `False` keeps legacy
  byte-identical behavior); injectable via `CASEEngine(audit_redactor=...)`.
- **(b) Envelope encryption (G4b)** — new `case_engine/audit/crypto.py`:
  `AEADPayloadCipher` (AES-256-GCM via the optional `cryptography` extra
  `pip install 'case-engine[crypto]'`), key from `CASE_ENGINE_LEDGER_KEY` or
  explicit bytes (`load_key`). JSONL/SQLite backends accept
  `payload_cipher=`; **encrypt-then-chain** — the hash chain commits to the
  ciphertext, so `verify_chain()` and `case-engine verify` stay **key-free**;
  `iter_events(decrypt=True)` decrypts (fail-closed `LedgerError` on wrong key
  or tampered ciphertext, which also breaks the chain). Enabled via
  `config.ledger_encryption=True`; the engine fails fast at construction when
  the key/cipher is unavailable.

**Verification:** `TestG4Redaction` (deterministic tokens, identity Noop,
engine-level redaction proves the raw email is absent from the ledger while
`[EMAIL_1]` is present, disabled = legacy) and `TestG4Encryption` (key loading,
JSONL/SQLite round-trips, wrong-key fail-closed, tampered ciphertext detected
by *both* AEAD auth and the chain, engine end-to-end encrypted JSONL,
missing-key failure, factory pass-through).

---

## P1 — Production-readiness gaps

> **Status (v2.2.0):** all nine P1 gaps (**G5–G13**) are **CLOSED** — see the
> "✅ CLOSED (v2.2.0)" blocks in each section for the implementing files and
> the verification evidence (`tests/unit/test_p1_gaps.py`, 55 tests, and
> `tests/unit/test_async_api.py`, 10 tests).

**P1 closure (v2.2.0) at a glance:**

| Gap | Implementation | Tests |
|---|---|---|
| G5 async API (phase 1) | `BaseLLMClient.generate_tool_call_async` (thread-offloaded default), `CASEStateMachine.run_async`, `CASEEngine.run_async` — non-blocking event loop, sync-twin semantics | `TestAsyncAdapterTwin` (2) + `TestAsyncEngine` (6) + `TestAsyncStateMachineGuards` (2) |
| G6 escalation round trip | `case_engine/escalation.py`: `EscalationHandoff` + `EscalationSink` protocol + File/Logging/Webhook sinks (best-effort default, strict opt-in); node_8 delivers + audits `HOOK_FIRED`; `CASEEngine.resume()` guided re-run | `TestG6Escalation` (14) |
| G7 token accounting | `ToolCallResult.usage` (normalized dict), state accumulator, `LLM_USAGE` audit event, `CASEResult.token_usage` (`TokenUsage`) | `TestG7TokenUsage` (5) |
| G8 provider resilience | `case_engine/llm/retry.py`: `RetryPolicy` (exponential backoff + jitter, Retry-After, 408/429/5xx/529 + timeout/connection), `call_with_retries` wired into both network adapters; content failures stay reflexion-routed | `TestG8Retry` (11) |
| G9 JSONL cross-process safety | advisory `flock` + tail re-read per append (`cross_process_safe=True` default; fcntl-less platforms degrade with a logged note); single-session-per-engine documented | `TestG9Flock` (2, incl. a two-process fork test) |
| G10 ledger lifecycle | size-based rotation (`max_bytes` → `.segN` segments, per-segment verification via `verify_all_segments` / `iter_all_events`), `audit/anchor.py` (`AnchorSink`, `FileAnchorSink`, `MemoryAnchorSink`, `make_anchor_record`), `AuditEvent.schema_version` + hashed `SESSION_START` payload field | `TestG10LedgerLifecycle` (7) |
| G11 observability | `case_engine/observability.py`: `Metrics` protocol, `NoopMetrics` (default), `InMemoryMetrics`, Prometheus text renderer, fail-open wrappers; wired into `EngineContext` + state machine + nodes | `TestG11Metrics` (6) |
| G12 result persistence | `case_engine/storage.py`: `ResultStore` protocol, `SQLiteResultStore`, `InMemoryResultStore`, factory; `EngineConfig.result_store_backend/path`; engine saves best-effort; `case-engine history` CLI | `TestG12ResultStore` (5) |
| G13 judge budget guard | `LLMBackedJudge.for_guardrails()` derives the deadline from `schema_validation_timeout_ms // 2` (250 ms floor); cumulative `audit_calls` counter | `TestG13JudgeBudget` (4) |

### G5 · Sync-only core: no async API, no streaming — ✅ CLOSED (v2.2.0, phase 1)
**Evidence:** `grep -rl "async def|stream=True" case_engine/` → **zero hits**; the
LangGraph adapter wires sync nodes only.
**Impact:** blocks FastAPI/websocket integrations (event-loop blocking), rules out
token-streaming UX, and LangGraph users running async graphs must shell out to
threads.
**Recommendation:** phase 1 — async mirror of `BaseLLMClient`
(`generate_tool_call_async`) + async node variants; phase 2 — streaming generate
with *incremental* gate validation (Pydantic partial validation per chunk).
Defer until a concrete embedding demands it; the pure-function core makes this
mechanical, not risky.

**✅ CLOSED (v2.2.0) — phase 1 (async mirror; streaming remains phase 2):**

- **Adapter twin** — `case_engine/llm/base.py :: BaseLLMClient.generate_tool_call_async`:
  default implementation offloads the sync call via `asyncio.to_thread`, so
  every adapter (mock, OpenAI, Anthropic, Ollama, vLLM, custom) is
  async-capable with identical semantics and error normalization; the
  awaiting event loop is never blocked by a provider round-trip.
- **Async machine** — `CASEStateMachine.run_async` (`pipeline/state_machine.py`):
  same `EDGES` guard, step bound and error semantics; nodes dispatched via
  `asyncio.to_thread` (metrics + topology checks identical).
- **Async facade** — `CASEEngine.run_async` (`engine.py`): full mirror of
  `run` (audit bracketing, state seeding incl. `llm_usage`, escalation
  packaging, result-store persistence).
- **Deliberate design note**: phase 1 guarantees the property integrators
  need (non-blocking loop, sync-twin outcomes — asserted by parity tests)
  with zero logic duplication; native-async SDK clients and incremental
  streaming validation remain documented phase-2 work
  (`docs/LLM_PROVIDERS.md`, `pipeline/langgraph_adapter.py` note).

**Verification:** `TestAsyncAdapterTwin` (result parity, loop responsiveness),
`TestAsyncEngine` (sync/async outcome parity, concurrent `asyncio.gather`
sessions on one engine, chain validity, escalation path async),
`TestAsyncStateMachineGuards` (topology unchanged, step guard trips async).

### G6 · Escalation (node_8) is a dead end — ✅ CLOSED (v2.2.0)
**Evidence:** node_8 packages a well-formed escalation result with partial traces
(verified), but there is **no outbound integration**: no webhook, ticket, email,
or human-review re-entry; `grep fastapi|flask|webhook|httpx case_engine/` → none.
**Impact:** escalations pile up as JSON; a human decision cannot flow back into
the engine (no resume-after-review path).
**Recommendation:** emit an `EscalationHandoff` (structured ticket object) and add
pluggable `EscalationSink` protocol (webhook/file/HTTP); add
`engine.resume(session_id, reviewer_verdict)` for the human-in-the-loop round trip.

**✅ CLOSED (v2.2.0) — exactly the recommended shape:**

- **Typed ticket** — `case_engine/escalation.py :: EscalationHandoff`
  (Pydantic, JSON-safe): session id, query, domain, selected models,
  retry count, validation errors, error detail, partial traces, reason,
  created-at, metadata.
- **Pluggable sinks** — `EscalationSink` protocol with three shipped
  implementations: `FileEscalationSink` (fsync'd JSONL),
  `LoggingEscalationSink` (structured WARNING), `WebhookEscalationSink`
  (stdlib `urllib` POST, no new dependency).  Delivery is **best-effort
  by default** (a down ticketing system must not fail a working
  escalation — logged ERROR, audited as `HOOK_FIRED ... delivered=false`);
  `strict=True` flips to fail-closed (`EscalationDeliveryError`).
- **node_8 wiring** — the escalation node builds the handoff from the
  escalation package, delivers via `ctx.escalation_sink` (injection or
  `config.escalation_sink_kind`), and audits the outcome.
- **Return path** — `CASEEngine.resume(prior, reviewer_verdict, ...)`: a
  guided re-run whose fresh session carries the verdict as operator
  context *data* (never instructions), with prior partial traces
  attached; the full constitution (PII gate, gates, rules, audit,
  reflexion) applies to the resumed session.  Accepts a `CASEResult` or
  a sink-loaded `EscalationHandoff`.

**Verification:** `TestG6Escalation` — sink round-trips, strict vs
best-effort contracts, local HTTP webhook delivery, node_8 hook audit on
success *and* on sink crash, resume from result and from handoff,
unusable-prior rejection, chain validity across the round trip
(demonstrated end-to-end by `examples/escalation_roundtrip.py`).

### G7 · No token/cost accounting persisted in results — ✅ CLOSED (v2.2.0)
**Evidence:** adapters compute usage (`openai_client.py` reads
`response.usage.total_tokens`; anthropic logs `input_tokens/output_tokens`) but
`CASEResult` has no usage fields (`grep usage|cost case_engine/schemas/pipeline.py`
→ none); quantities live only in log lines.
**Impact:** impossible to answer "what did this session cost?" without log scraping.
**Recommendation:** add `TokenUsage` (prompt/completion/total per request, summed
per session) to `CASEResult`; adapters already have the data in hand.

**✅ CLOSED (v2.2.0):** `ToolCallResult.usage` carries the normalized
`{prompt_tokens, completion_tokens, total_tokens}` dict (OpenAI and
Anthropic adapters extract it; `None` when unreported); node_1/node_4
accumulate into the JSON-safe `state["llm_usage"]`; the terminal nodes
emit an `LLM_USAGE` audit event (counts only — never content); the engine
maps the accumulator into `CASEResult.token_usage` (`TokenUsage`, with
`total` recomputed rather than trusted).  Sessions that escalate still
eimit the event — cost visibility does not depend on success.

**Verification:** `TestG7TokenUsage` — aggregation math, call counting
without provider usage, ledger event presence and payload, negative-value
clamping.

### G8 · No provider resilience layer (retry/backoff/429) — ✅ CLOSED (v2.2.0)
**Evidence:** `grep backoff|retry|max_retries case_engine/llm/` → no hits; errors
normalize into `LLMProviderUnavailableError` and rely on the *pipeline* reflexion
loop for retries (which re-sends the identical request immediately, with no
jitter, and consumes reflexion budget that exists for validation failures).
**Impact:** transient 429/5xx burns reflexion retries and can escalate sessions
that a 500ms exponential backoff would have saved.
**Recommendation:** adapter-level retry with exponential backoff + jitter for
retryable statuses (429, 5xx, timeouts), honoring `Retry-After`; keep reflexion
budget for genuine content failures. ~80 lines + tests.

**✅ CLOSED (v2.2.0):** new `case_engine/llm/retry.py` (~300 lines with
docs): `RetryPolicy` (validated, frozen), `classify_transport_failure`
(status codes 408/429/5xx/**529** + timeout/connection class-name hints),
`retry_after_seconds` (attribute + header probing, numeric only),
`call_with_retries` / `acall_with_retries`.  Both network adapters wrap
exactly their transport call with the policy (constructor params
`retry_attempts=3, retry_initial_backoff_s=0.5, retry_max_backoff_s=8.0`;
`retry_attempts=1` disables — the legacy behaviour).  Exhausted budgets
normalize into `LLMProviderUnavailableError` at the existing single
wrapping boundary — no new exception types for callers.  Content failures
(`LLMResponseError`) are never retried: reflexion keeps its budget for
what it exists for.

**Verification:** `TestG8Retry` — policy validation, backoff curve/cap,
Retry-After override+clamp, classification matrix, fail-fast vs retried,
budget exhaustion, both adapters (transient recovery → parsed tool call;
permanent 429/529 → normalized error after exactly N attempts), usage
extraction.

### G9 · Thread/process safety of the engine and JSONL ledger — ✅ CLOSED (v2.2.0)
**Evidence:** only `SQLiteLedger` holds a lock (in-process, documented); JSONL
append is single-threaded by assumption (no file lock); `CASEEngine`/state machine
carry no synchronization (`grep threading|Lock case_engine/engine.py case_engine/pipeline/state_machine.py` → none).
**Impact:** two threads sharing one engine can interleave session state; two
*processes* appending to one JSONL corrupt chain continuity (each replays its own
tail).
**Recommendation:** document single-session-per-engine explicitly (cheapest), add
an advisory `flock` to JSONL append for cross-process safety, and test the SQLite
backend as the recommended multi-writer choice.

**✅ CLOSED (v2.2.0):**

- **JSONL advisory flock** — `JSONLLedger(cross_process_safe=True)`
  (default): every append takes an exclusive `fcntl.flock` on the ledger
  file and re-reads the file tail *under the lock*, adopting an externally
  advanced chain instead of forking it (INFO-logged).  Platforms without
  `fcntl` degrade to single-writer with a one-time DEBUG note (SQLite
  documented as the multi-writer choice).  `cross_process_safe=False`
  restores the cheapest legacy path.
- **Documentation** — engine and ledger docstrings now state the
  concurrency contract explicitly: one engine instance drives one session
  at a time (thread-safe ledgers; deterministic nodes are copy-on-write);
  concurrent callers should use one engine per session or
  `run_async` (G5) for I/O overlap; SQLite for multi-process writes.
- **Session accounting** — sessions remain bracketed by
  `SESSION_START`/`SESSION_COMPLETED`/`SESSION_FAILED` regardless of
  writer interleaving (the two-process test asserts one unbroken chain).

**Verification:** `TestG9Flock` — two forked processes appending 5 events
each to one file produce a 10-event, fully verifiable chain (no forked
hashes); legacy mode still verifies.

### G10 · Ledger lifecycle missing: rotation, retention, external anchoring — ✅ CLOSED (v2.2.0)
**Evidence:** JSONL grows unboundedly; no rotation/compaction; no external
timestamp anchoring (`grep rotate|rfc3161|timestamp_authority case_engine/audit/`
→ only "genesis anchor" wording); `SCHEMA_VERSION` exists (`version.py`) but there
is **no migration machinery** for ledgers written by older schema versions.
**Impact:** long-running deployments hit unbounded files; the tamper-evidence
story currently ends at the local file (an attacker with write access can rewrite
the whole chain and re-anchor at genesis — verified: `_replay` trusts the file's
first record).
**Recommendation:** size-based rotation with per-segment chain verification;
periodic head-hash anchoring to an external authority (RFC 3161 TSA, or even a
committed Git ref / S3 object lock) — this is what elevates the chain from
"self-consistent" to "externally provable"; add a `schema_version` field to
`AuditEvent` for forward migration.

**✅ CLOSED (v2.2.0):**

- **Rotation** — `JSONLLedger(max_bytes=N)`: overflow-free appends rotate
  the active file to `<stem>.seg<N><suffix>` and re-anchor at genesis
  (per-segment chains; rotation numbering survives restarts).  Verify per
  segment (`verify_all_segments()`), iterate across history
  (`iter_all_events()`); `verify_chain()` keeps meaning the active
  segment.  A single event larger than `max_bytes` gets its own segment
  rather than an error.
- **External anchoring** — new `case_engine/audit/anchor.py`:
  `AnchorSink` protocol + `FileAnchorSink` (fsync'd JSONL) +
  `MemoryAnchorSink`; `JSONLLedger.anchor(note=...)` commits the current
  head hash on demand and rotation auto-anchors the closed segment when a
  sink is configured (anchor written *before* the new segment accepts
  events).  The record is the external-verifier contract — point a TSA,
  Git commit or object-lock bucket at it to upgrade from
  self-consistent to externally provable.
- **Schema version** — `AuditEvent.schema_version` (default 1) added for
  forward migration; the frozen hash recipe is untouched (old ledgers
  replay and verify unchanged — regression-tested), and the per-session
  `SESSION_START` payload now carries `audit_event_schema_version`
  *inside* the hashed material so the version itself is tamper-evident.

**Verification:** `TestG10LedgerLifecycle` — multi-segment rotation all
valid, rotate+manual anchor records, no-sink no-op, cross-segment
iteration, schema version present, **v2.1.0-format legacy ledger replays
and extends cleanly**, invalid threshold rejected.

### G11 · No observability hooks (metrics/tracing) — ✅ CLOSED (v2.2.0, counters/timers; OTel spans remain optional future work)
**Evidence:** logging is excellent (286 structured sites), but there are no
counters/histograms and no trace spans (`grep prometheus|opentelemetry|otel`
→ none).
**Impact:** cannot alert on reflexion rate, gate rejections, or p99 node latency
without log parsing.
**Recommendation:** optional OTel span per node (no-op provider by default, zero
new hard deps) + a small `Metrics` protocol (counters only) wired into
`EngineContext`; export `case_engine_*` Prometheus text via an optional extra.

**✅ CLOSED (v2.2.0) — metrics half; OTel spans stay an optional extra:**

- `case_engine/observability.py`: `Metrics` protocol (`incr`/`observe`/
  `snapshot`), `NoopMetrics` (default — zero overhead), `InMemoryMetrics`
  (thread-safe, deep-copied snapshots), `render_prometheus` (text
  exposition, counters + summaries, no dependency), and fail-open
  wrappers (`safe_incr`/`safe_observe`) so a broken sink can never fail a
  session.
- Wired at: `EngineContext.audit` (per-event counters), the state machine
  (per-node call counts + latency), node_1/node_4 (LLM latency + adapter
  failures + trace counters), node_6 (reflexion retries), node_8
  (escalations), engine (result-store outcomes, resumes).  Labels are
  bounded-vocabulary (node ids, model ids, event types) — never
  free-text.
- `EngineConfig.log_level="DEBUG"` installs an `InMemoryMetrics` for free
  so debug sessions can answer "how many gate rejections?" without extra
  wiring; injection wins otherwise.  Prometheus export is
  `render_prometheus(engine.metrics)` behind any HTTP endpoint the host
  already runs — no new optional extra required.

**Verification:** `TestG11Metrics` — counter/observation storage,
PROMETHEUS text shape, no-op silence, sink-crash fail-open, live engine
instrumentation, protocol conformance.

### G12 · Session results are not persisted as first-class records — ✅ CLOSED (v2.2.0)
**Evidence:** `CASEResult` is returned/printed but never stored; the only durable
record is audit events (payload digests, designed for evidence, not for query by
session).
**Impact:** no "show me last Tuesday's sessions" workflow without full-ledger scan.
**Recommendation:** optional `ResultStore` backend (SQLite default, same pattern
as the ledger trio); `case-engine history --since ...` becomes trivial afterwards.

**✅ CLOSED (v2.2.0) — exactly that:** `case_engine/storage.py` defines
`ResultStore` (save/get/iter_since/close), `SQLiteResultStore`
(stdlib sqlite3, projected query columns + full JSON document,
thread-safe, upsert idempotent), `InMemoryResultStore` and the
`create_result_store` factory.  Opt-in via
`EngineConfig(result_store_backend=...)` or constructor injection; the
engine saves completed results (success *and* escalations) best-effort —
a store failure is logged and never fails a session.  The ledger remains
the authoritative record; the store is the queryable index.
`case-engine history --store PATH [--since ISO] [--limit N] [--session ID]
[--json]` ships in the CLI, and `run --result-store PATH` enables
persistence per invocation.

**Verification:** `TestG12ResultStore` — factory contract, SQLite
round-trip/filters/idempotency, engine-configured persistence, default
none, store-crash fail-open (plus a live CLI smoke in the release gate).

### G13 · Judge path lacks an explicit deadline + budget guard — ✅ CLOSED (v2.2.0)
(Subset of G3 but cheap and separate: the judge call is the only unbounded
network call inside validation.) Cap judge latency to a fraction of
`schema_validation_timeout_ms` and fail-open on overrun — current fail-open
degradation (verified, veto-only preserved) makes this safe by design.

**✅ CLOSED (v2.2.0):** `LLMBackedJudge.for_guardrails(guardrails)` derives
the deadline as `schema_validation_timeout_ms // 2`, floored at 250 ms —
the judge can never outlive half its host validation budget, and the
existing fail-open degradation (veto withheld, WARNING logged) covers the
overrun path unchanged.  A cumulative `LLMBackedJudge.audit_calls` counter
exposes judge invocation volume for budget monitoring.  The G3 hard
deadline machinery itself shipped in v2.1.0.

**Verification:** `TestG13JudgeBudget` — derived deadline (default budget,
tiny-budget floor), `audit_calls` increment, deadline-overrun fail-open
verdict.

---

## P2 — Developer experience & ecosystem gaps

> **Status (v2.3.0):** all P2 gaps (**G14–G21**, with G20 closed by
> documented decision) are **CLOSED** — see the "✅ CLOSED (v2.3.0)"
> blocks in each section for the implementing files and the
> verification evidence (`tests/unit/test_p2_gaps.py`, 39 tests;
> ruff + mypy clean; all gates green).

**P2 closure (v2.3.0) at a glance:**

| Gap | Implementation | Tests |
|---|---|---|
| G14 CI + lint/type gates | `.github/workflows/ci.yml` (3.11/3.12/3.13: compileall → ruff → mypy → pytest → doctests → examples → CLI smoke), `[tool.ruff]`/`[tool.mypy]` in pyproject; codebase at zero ruff / zero mypy findings | `TestG14CITooling` (3) |
| G15 registry packs | `registry/pack.py` JSON contract (`schema_version` pinned, unknown keys rejected), `CognitiveRegistry.load_pack(overlay=)`/`.dump_pack`, `RegistryPackError`; same entry models + consistency checker | `TestG15RegistryPacks` (9) |
| G16 serving story | `case_engine/server.py` (`case_engine[server]`): `/health`, `POST /sessions` (async engine), `GET /sessions/{id}`, `GET /ledger/verify`; lazy FastAPI; `Dockerfile` + `docker-compose.yml` (non-root, `/data` volume) | `TestG16Server` (8) |
| G17 CLI verbs | `doctor` (registry/ledger/provider health), `init` (schema-pure, loader-validated template), `export` (JSON/CSV from the G12 store) | `TestG17CliVerbs` (6) |
| G18 repo hygiene | `CONTRIBUTING.md`, `SECURITY.md`, license resolved to Apache-2.0 OR Commercial (LICENSE + pyproject SPDX + classifier) | presence-asserted in `TestG14CITooling` |
| G19 i18n seam | `messages.py`: `MessageCatalog`/`DictMessageCatalog`/`set_catalog`/`msg` (never raises); wired in `run_rule` (`vr.<ID>.pass/fail`) + escalation response; inert by default | `TestG19Messages` (9) |
| G20 Python floor | Decision: keep `>=3.11` (3.11/3.12/3.13 in CI); documented in EXTENDING.md + this section — no speculative back-port | `TestG20CompatDecision` (1) |
| G21 adversarial tour | `examples/adversarial_tour.py`: 5 offline stages (gate rejection → reflexion recovery ×2 → escalation → tamper detection) | executed in CI example loop |

### G14 · No CI pipeline, no lint/type gates — `S`
**✅ CLOSED (v2.3.0):** `.github/workflows/ci.yml` runs the full gate
sequence (compileall, `ruff check`, `mypy case_engine/`, pytest,
doctests, all examples, CLI smoke) on Python 3.11/3.12/3.13 — fully
hermetic, no secrets needed.  Tool config lives in `pyproject.toml`
(`[tool.ruff]` curated rule set: E9/F/W605/I/UP/RUF100 with documented
exceptions; `[tool.mypy]` with `check_untyped_defs`).  The codebase
was brought to **zero ruff findings and zero mypy errors**, fixing real
latent defects along the way (missing `Any` import in
`pipeline/context.py`, untyped JSONL ledger handles, a double
`Regex.search` in VR005, non-narrowed `raise` in the retry drivers,
`CASEState | dict` annotations on the engine's result projections).
Evidence: `TestG14CITooling` asserts the workflow + config presence;
`ruff check case_engine/ tests/ examples/` and `mypy case_engine/`
are both clean locally.

**Evidence:** no `.github/`, no `ruff|mypy|pyright|black` config in `pyproject.toml`;
quality currently rests on the 145-test suite alone.
**Recommendation:** GitHub Actions matrix (3.11/3.12/3.13): pytest + doctests +
`ruff check` + `mypy --ignore-missing-imports`; ~50 lines of YAML. This is the
highest-leverage hygiene gap in the list.

### G15 · Registry packs are hardcoded Python — no external pack format — `M`
**✅ CLOSED (v2.3.0):** `case_engine/registry/pack.py` defines the JSON
pack contract — one UTF-8 object with `schema_version` (pinned to
`SCHEMA_VERSION`), `generated_by`, `models`, `rules`,
`routing_matrix`; unknown top-level keys and wrong versions fail
closed with `RegistryPackError` (new `RegistryError` subclass carrying
`pack_path` in `context`).  Entries are validated by the same frozen
`MentalModelDefinition` / `ValidationRuleDefinition` models
(`extra=forbid`) and the merged result passes the same
`_check_consistency` checker as code-defined data.  API:
`CognitiveRegistry.load_pack(path)` (complete), `load_pack(path,
overlay=True)` (id-merged over defaults — tune one model without
forking the constitution), `CognitiveRegistry.dump_pack(path)`
(round-trip property is regression-tested), plus module-level
`load_registry_pack` / `dump_registry_pack` /
`PACK_TOP_LEVEL_KEYS` re-exported from `case_engine.registry`.
Authored guide: `docs/EXTENDING.md` §"Registry packs".
Demo: `examples/registry_pack_demo.py`.  Entry-point discovery
(`case_engine.packs`) remains documented future work (explicit
`load_pack` covers the review/versioning need).

**Evidence:** data lives in `model_data_a/b.py`, `rule_data.py`; loading is
`CognitiveRegistry.load_default()` only; no YAML/JSON pack import, no
`importlib`/entry-point discovery (`grep yaml|entry_points|importlib case_engine/registry/`
→ none).
**Impact:** organizations cannot version/review their own model packs without
forking Python; EXTENDING.md documents code edits only.
**Recommendation:** define a JSON registry-pack contract (`SCHEMA_VERSION`
already anticipates this), `CognitiveRegistry.load_pack(path)`, validate with the
existing consistency checker; entry-point discovery (`case_engine.packs`) second.

### G16 · No serving story: REST/OpenAPI, Docker, compose — `M`
**✅ CLOSED (v2.3.0):** `case_engine/server.py` behind the
`case_engine[server]` extra (`fastapi>=0.110`, `uvicorn>=0.29`).
`create_app()` exposes `GET /health`, `POST /sessions` (full pipeline
via the G5 async path — the event loop is never blocked),
`GET /sessions/{id}` (bounded FIFO in-process mirror, oldest evicted)
and `GET /ledger/verify` (on-demand `verify_audit_chain`).  FastAPI
imports are lazy — the core package never needs it; a missing install
raises a documented `ImportError` naming the pip extra.  Error
mapping preserves fail-closed semantics: schema rejections → 422,
`LLMProviderUnavailableError` → 503, other `CASEError` → 500,
unexpected → 500 with a generic body.  Request bodies are typed
(`RunRequest`, `extra=forbid`, query length bounded); logs carry
query *length*, never content.  `Dockerfile` (python:3.12-slim,
non-root uid 10001, `/data` evidence volume, HEALTHCHECK against
`/health`) and `docker-compose.yml` (persistent `case-data` volume).
Evidence: `TestG16Server` (8 tests via `TestClient`, incl. the
bounded mirror and the ImportError message).

**Evidence:** no server code, no Dockerfile. The engine is a library+CLI only.
**Recommendation:** optional extra `case_engine[server]`: thin FastAPI wrapper
(`POST /sessions`, `GET /sessions/{id}`, `GET /ledger/verify`) reusing the async
work from G5; Dockerfile + compose with a mounted ledger volume.

### G17 · CLI gaps: no `doctor`, `init`, `history`, `export` — `S`
**✅ CLOSED (v2.3.0):** `history` landed with G12 (v2.2.0); this round
adds the remaining three verbs.  `case-engine doctor` health-checks
the registry (id contiguity MM001–015/VR001–016, routing domains),
the ledger (hash-chain verification; missing file = *warn* for fresh
installs, tampering = *fail* → exit 1) and the provider (`--ping`
constructs the client without an API call; `--json` machine mode).
`case-engine init` writes a schema-pure `config.json` template
(safe under `extra=forbid`) and proves it round-trips through the
strict `EngineConfig.from_file` loader before reporting success;
overwrite requires `--force`.  `case-engine export` streams the G12
result store to JSON (full documents) or CSV (projected columns:
session id, timestamps, domain, models, retries, verdicts, tokens)
to a file or stdout, creating parent dirs.  Evidence:
`TestG17CliVerbs` (6 tests incl. tamper detection via `doctor` and
both export formats).

**Evidence:** verbs are exactly `run|demo|models|show|verify|config` (`cli.py:93-129`).
**Recommendation:** `doctor` (registry self-check, ledger health, provider ping),
`init` (write commented config.json), `history`/`export` (after G12/G5-SIEM).

### G18 · Repo hygiene: no CHANGELOG / CONTRIBUTING / SECURITY — `S`
**✅ CLOSED (v2.3.0):** `CHANGELOG.md` has existed and is maintained
per release (now incl. this round).  New: `CONTRIBUTING.md` (dev
setup, the six-command verification gate, the six ground rules —
fail-closed, log-safety, deterministic control plane, tests-with-
changes, zero hard deps, stable ids — and the PR checklist) and
`SECURITY.md` (supported-version table, non-public disclosure
process, the engine's threat-model table, and the honest caveats:
ledger write-access, multi-writer guidance, judge fail-open scope,
PII policy ownership).  The license is resolved to the actual dual
text: `LICENSE` already carried Apache-2.0-OR-Commercial wording;
`pyproject.toml` now declares
`license = { text = "Apache-2.0 OR Commercial (dual-licensed, see LICENSE)" }`
plus the Apache OSI classifier.  Presence is asserted by
`TestG14CITooling::test_doc_hygiene_files_shipped`.

**Evidence:** `ls` shows LICENSE/README only. The license field is a placeholder
string ("Proprietary / Open-Source Dual-License") without the dual-license texts.
**Recommendation:** add CHANGELOG.md (the enhancement history is real content),
CONTRIBUTING.md (conventions doc already exists as the backbone), SECURITY.md
(disclosure + the TEE/PII caveats from G1/G4), and resolve the license to actual
texts (e.g. Apache-2.0 OR commercial).

### G19 · i18n: all validation messages English-only — `S`
**✅ CLOSED (v2.3.0):** `case_engine/messages.py` provides the seam
exactly as recommended: `MessageCatalog` protocol +
`DictMessageCatalog`, process-level `set_catalog` / `reset_catalog`,
and `msg(key, default, **params)` — which **never raises** (a broken
catalog logs a WARNING and falls back to the engine's English text;
rendering is the one deliberate fail-open in the engine, because a
missing translation must never stop a session).  Wired at two choke
points: `run_rule` resolves `vr.<ID>.pass` / `vr.<ID>.fail` for every
validation verdict (the strings that enter reflexion feedback), and
the infrastructure-escalation response resolves the parameterized
`escalation.infra_failure` (`{error_type}`, `{error}`, `{session_id}`).
Inert by default: with no catalog installed every string is
byte-identical to pre-2.3.0 output.  Evidence: `TestG19Messages`
(9 tests: default inertness, overrides + params, fallbacks, throwing
catalogs, and the two integration choke points incl. a German VR001
override).

Failure strings are user-facing (they enter reflexion feedback and escalations);
non-English adopters need a message-catalog seam. Low urgency, worth a `messages.py`
indirection when the first non-English adopter appears.

### G20 · Python floor 3.11+ excludes 3.10 — `S`
**✅ CLOSED BY DECISION (v2.3.0):** the recommendation ("decide based
on adopter demand, don't pay speculatively") was adopted: the floor
stays `>=3.11`, and CI now *tests* the claim across 3.11/3.12/3.13
(G14).  Rationale: the codebase freely uses PEP 604 unions and
3.11-era idioms (autofixed to modern syntax by the UP rules), so a
3.10 back-port is a real maintenance commitment, not a formality — it
will be reconsidered when a concrete adopter demands it.  Contract:
`TestG20CompatDecision::test_python_floor_unchanged` pins
`requires-python = ">=3.11"` in `pyproject.toml`.

**Evidence:** `requires-python = ">=3.11"`; code uses `X | Y` unions and
`tomllib`-era stdlib only. 3.10 support is likely a `from __future__ import
annotations` + minor changes exercise — decide based on adopter demand, don't
pay speculatively.

### G21 · No `case-engine` demo coverage for the judge & failure-injection paths — `S`
**✅ CLOSED (v2.3.0):** `examples/adversarial_tour.py` walks the full
adversarial surface offline in five stages: (1) hallucinated tool →
hard fail-closed `ToolCallRejectedError` (nothing silently accepted;
evidence in the ledger), (2) schema-violating arguments → reflexion
recovery, (3) missing tool call → reflexion recovery, (4) persistent
misbehavior → reflexion budget exhausted → typed human escalation,
(5) payload tampering → `verify_chain()` detects the mutation with the
exact sequence number.  Every stage prints its observable outcome and
the run is part of the CI example loop.

The MockLLM can inject hallucinated tools, malformed payloads, and refusals
(verified) and the judge degrades fail-open (verified in tests), but no *example*
demonstrates the full adversarial tour; the reflexion demo covers one cycle only.
**Recommendation:** `examples/adversarial_tour.py` — 60 lines, high teaching value.

---

## P3 — Assurance & research gaps

### G22 · No property-based/fuzz testing of the gate and rules — ✅ CLOSED (v2.4.0)
`tests/unit/test_property_gate.py` (Hypothesis, `derandomize` CI profile,
skips gracefully without the package): adversarial argument strategies
(extra keys, wrong types, 50-level nesting, zero-width/bidi unicode, NaN/±inf,
2^63 ints) assert the gate (a) never crashes on arbitrary input, (b) never
approves a payload that is not schema-conformant (`extra="forbid"` holds under
fuzzing), (c) always raises `ToolCallRejectedError` for unknown tool names,
(d) rejects every single-field mutation (add / drop / type-flip) of a gold
payload; `canonical_json` round-trip + key-order + non-ASCII properties and
`compute_event_hash` stability/sensitivity properties.

### G23 · No adversarial prompt-injection suite — ✅ CLOSED (v2.4.0)
`tests/adversarial/corpus.py`: **31 curated payloads** (INJ-01…INJ-31) across 8
categories — tool-name hallucination, spec leak, rule baiting, delimiter
smuggling, roleplay jailbreak, encoding obfuscation (base64 / zero-width /
bidi / NUL), authority forgery, data exfiltration.  Wired end-to-end against
node_4→node_5 by `tests/integration/test_adversarial_injection.py`
(no crash, defined terminal state, chain verifies, PII flagged per G1,
synthesis only from validated traces) and human-reviewable via
`python -m tests.adversarial.report`.  Malicious *completions* are simulated
with a poisoned mock client: extra-field + directive-stuffed payloads are
gate-rejected and healed by reflexion; persistent poisoning escalates;
hallucinated tools are enforcement faults that now close with a
`SESSION_FAILED` audit event (engine fix in this release).

### G24 · No concurrency tests — ✅ CLOSED (v2.4.0)
`tests/integration/test_concurrency.py`: 8 threads × 4 sessions on one engine
(unique session ids, chain verifies); 6 threads sharing one JSONL-backed
engine; 8 threads appending directly to one JSONLLedger (every line clean JSON,
unique seq); **4 spawn-processes × 5 appends on one JSONL file** — exactly 20
lines, unique seq 1..20, reopened chain verifies (clean serialization, no
silent corruption); corrupt tail / mid-file corruption raise `LedgerError`;
SQLite threaded appends verify; JSONL→SQLite re-import preserves payloads.

### G25 · No performance benchmarks — ✅ CLOSED (v2.4.0)
`bench/run_bench.py` publishes per-node p50/p95/p99 (via the G11
`case_engine_node_latency_ms` hooks), full-pipeline latency + throughput with
the offline mock provider, gate overhead (approve vs reject paths), hash-chain
primitive cost, and per-backend append cost (memory / JSONL buffered / JSONL
fsync / SQLite) with p50/p95/p99/mean/min/max.  Profiles `smoke` (CI) and
`standard`; JSON + markdown output; smoke-regression-tested in
`tests/integration/test_bench.py`.  Numbers are machine-indicative by design
(the report embeds platform metadata) — capacity claims must cite a run.

### G26 · Coverage thin spots in 4 files — ✅ CLOSED (v2.4.0)
Package total **88 % → 95 %**.  Trust boundary: `audit/anchor`, `audit/base`,
`audit/crypto`, `audit/memory_ledger`, `audit/sqlite_ledger`,
`enforcement/gate`, `enforcement/tool_factory`, `logging_setup`,
`registry/routing`, `pipeline/state_machine`, `validation/engine` at **100 %**;
`audit/jsonl_ledger` 99 %.  Thin files: `llm/openai_client.py` 99 %,
`logging_setup.py` 100 %, `registry/routing.py` 100 %, `pipeline/nodes.py`
96 %.  Evidence: `tests/unit/test_p3_coverage_gaps.py` (50+ targeted
edge tests, each pinning a documented fail-closed branch — flock loss,
post-failure tail adoption, rotation/anchor failures, corrupt segments,
SDK-missing adapters, webhook delivery failure, pack/config errors).

### G27 · Provider contract tests run against stubs only — ✅ CLOSED (v2.4.0)
`tests/cassettes/openai_chat_completions_cassette.json` and
`anthropic_messages_cassette.json` pin the recorded wire envelopes
(`meta.pinned_sdk`, refresh policy, `expected` projection).  Three contract
layers in `tests/unit/test_provider_contracts.py`: (1) envelope-contract
assertions (field/type drift fails loudly), (2) adapter replay through a
cassette-driven stub client (parsed `ToolCallResult` must match the recording;
degradation variants raise exactly `LLMResponseError`/`LLMAdapterError`),
(3) real-SDK validation via `ChatCompletion.model_validate` /
`Message.model_validate` when the optional SDKs are installed — the drift
alarm that previously could only fail in the field.

### G28 · No mutation testing / semantic diffing of rule logic — ✅ CLOSED (v2.4.0)
`tools/mutation_run.py` — deterministic, zero-dependency mutation runner
(token-level `<`⇄`<=`, `>`⇄`>=`, `==`⇄`!=`, `and`⇄`or`, `True`⇄`False` over
`validation/rules.py` + `enforcement/gate.py`; 100 sites), per-target test
scoping, restore-or-die, chunked resumption; `[tool.mutmut]` configured for
the canonical toolchain.  First run scored **0.63**, exposing 22 unpinned
boundaries; `tests/unit/test_p3_mutation_pins.py` (19 pins) hardens them to
**0.98** (98/100 killed; the 2 survivors are documented equivalent mutants in
`docs/MUTATION_BASELINE.md`).  Run once per release:
`python tools/mutation_run.py`.

---

## Verified non-gaps (checked, found already correct)

For balance — these were explicitly audited and need **no** work:

- **Strategy 1 (zero-trust gate):** every tool call re-validated, `extra="forbid"`,
  unknown tool = enforcement fault, no prior-validation trust.
- **Strategy 2 (deterministic control flow):** EDGES is the single source of truth;
  illegal transitions raise `TopologyViolationError`; LLM has zero agency.
- **Strategy 3 (reflexion):** falls back to the *failed* node, budget exhaustion
  escalates with partial traces; judge is strictly veto-only and degrades fail-open
  (deterministic rules remain fail-closed).
- **Audit chain:** SHA-256 chaining verified end-to-end; tamper detection tested;
  three interchangeable backends; fail-closed appends.
- **Judge semantic coverage:** exactly VR001/VR002/VR005/VR012/VR014 as designed.
- **Mock determinism:** 145 tests, offline, <1s — reproducible failure injection
  (fail_next, hallucinated tools, malformed payloads).
- **Docstrings/logging:** 148+/150+ functions documented; 290+ structured log sites;
  log-safety rules hold (no secrets, no payload content above DEBUG).
- **`allow_unvalidated_passthrough`:** implemented with loud warnings, default False.
- **P0 closures (v2.1.0):** TEE/PII gate (G1), fallback policies (G2), judge
  deadline (G3), redaction + encryption at rest (G4) — all regression-tested in
  `tests/unit/test_p0_gaps.py` (26 tests) and demonstrated in
  `examples/pii_protection.py`.

---

## Suggested sequencing (if resourced incrementally)

1. ~~Week 1 (hygiene + constitution):~~ G14 CI gates → ~~G2 fallback_behavior~~ *(done)* → ~~G7 token usage~~ *(done v2.2.0)* → ~~G13 judge deadline~~ *(done)* → G18 repo hygiene.
2. ~~Week 2 (data safety):~~ ~~G1 PII detection + TEE audit event~~ *(done)* → ~~G4 redaction hook~~ *(done)* → ~~G9 flock + concurrency tests (G24)~~ *(flock done v2.2.0; G24 suite remains open)*.
3. ~~**Weeks 3–4 (resilience + evidence):**~~ ~~G8 retry/backoff → G10 rotation +
   external anchoring → G12 result store → G17 CLI round-out~~ *(G8/G10/G12 +
   `history` CLI done v2.2.0; G17 partially — `history` shipped, `init`/`export`
   remain open)*.
4. ~~**Then, by demand:**~~ ~~G5 async/streaming → G6 escalation sinks~~
   *(G5 phase 1 + G6 done v2.2.0)* → G15 pack
   format → G16 server → ~~G22/G23 adversarial hardening~~ *(done v2.4.0)*.

*Regenerate after each landing: `FILE_REFERENCE.md` (AST), test suite, and this
document's evidence greps — a stale gap list is worse than none.*
