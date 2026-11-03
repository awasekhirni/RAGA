# File & API Reference

Complete, generated-and-curated reference of every file, class and function in
`case_engine`. For each module below you get its role, its public contents and
one-line summaries; for **full** documentation — extended prose, `Args`,
`Returns`, `Raises`, invariants and executable `Example:` doctests — open the
module itself: every docstring in this package is written to Google style and
the examples are doctest-verified.

**How to read this document**

- Layers appear in architectural reading order (surface → data → knowledge →
  enforcement → validation → providers → audit → control plane).
- Within a layer, modules are listed with line counts and their public API.
- Cross-cutting rules (logging levels, error-handling contract, fail-closed
  semantics) live in [`CONVENTIONS.md`](CONVENTIONS.md); architecture diagrams
  and spec deviations in [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Companion docs**

| Document | Contents |
|---|---|
| `README.md` | Quickstart, feature overview, CLI usage |
| `ARCHITECTURE.md` | System design, the three enforcement strategies, spec deviations |
| `PIPELINE.md` | The 8 nodes, EDGES table, reflexion semantics |
| `MENTAL_MODELS.md` | MM001–MM015 catalog with guidance |
| `VALIDATION_RULES.md` | VR001–VR016 catalog with pass/fail criteria |
| `AUDIT_LEDGER.md` | Hash-chain design, backend selection, tamper evidence |
| `LLM_PROVIDERS.md` | Adapter matrix, MockLLM failure injection |
| `EXTENDING.md` | Adding models, rules, providers, backends |
| `CONVENTIONS.md` | Logging, error-handling and documentation standards |

## Package map

```
case_engine/
├── __init__.py        package facade (re-exports)
├── audit/      hash-chained ledger (JSONL / SQLite / Memory)   [2300 LOC / 7 files]
├── enforcement/ SchemaGate + ToolFactory (zero-trust)   [455 LOC / 3 files]
├── llm/        provider adapters (mock/openai/anthropic/ollama/vllm)   [1887 LOC / 7 files]
├── pipeline/   8 nodes + state machine + LangGraph adapter   [2311 LOC / 5 files]
├── registry/   MM001-MM015 + VR001-VR016 + routing matrix   [2274 LOC / 7 files]
├── schemas/    typed Pydantic contracts   [1378 LOC / 5 files]
├── validation/ 16 deterministic rules + LLM judge   [1961 LOC / 4 files]
└── engine.py, cli.py, config.py, privacy.py, exceptions.py, logging_setup.py, version.py, __main__.py   [5039 LOC]
```

**Documentation & instrumentation coverage** (machine-verified):

- Functions/methods in package: **271**
- With Google-style docstrings: **271** (100%)
- Structured logging call sites (`log.debug/info/warning/error`): **444**
- Doctests executable: 68+ passed, rest intentionally skipped (`+SKIP`) —
  run with `pytest --doctest-modules case_engine`
- P0 gap closures (v2.1.0): G1 TEE/PII gate, G2 fallback policies, G3 judge deadline, G4 redaction + encryption — see docs/GAP_ANALYSIS.md
- P1 gap closures (v2.2.0): G5 async API, G6 escalation round trip, G7 token accounting, G8 retry/backoff, G9 JSONL flock, G10 rotation + anchoring, G11 metrics, G12 result store + history CLI, G13 judge budget guard
- P2 gap closures (v2.3.0): G14 CI + ruff/mypy gates, G15 JSON registry packs, G16 FastAPI server + Docker, G17 doctor/init/export CLI, G18 repo hygiene, G19 i18n message seam, G20 3.11-floor decision, G21 adversarial tour example

---

## 1. Surface layer — facade, configuration & developer interface

This is everything an integrator touches directly. `engine.py` is the one-class facade guaranteeing the four construction-level contracts (schema-gated calls, deterministic topology, reflexion-then-escalate, hash-chained audit); `cli.py` exposes the same capabilities as an offline-capable command line; `config.py` resolves guardrails and provider options with `extra=forbid` strictness; `logging_setup.py` provides the structured-logging namespace every module logs through; `exceptions.py` defines the single `CASEError` hierarchy that the whole package raises; `privacy.py` (v2.1.0) supplies the PII detection and audit-redaction primitives behind the G1/G4 data-protection guarantees; `observability.py` (v2.2.0, G11) the counters/timers sinks, `storage.py` (v2.2.0, G12) the optional session-result store, `escalation.py` (v2.2.0, G6) the typed escalation handoffs and delivery sinks, `messages.py` (v2.3.0, G19) the i18n message-catalog seam (never-raises `msg` resolver), and `server.py` (v2.3.0, G16) the optional FastAPI serving layer behind the `server` extra; `version.py` and the two entry modules round out identity and import.


#### `case_engine.py` — 98 LOC

**Role:** C.A.S.E. — Cognitive Architectural Scaffolding & Enforcement Engine.

**Contents:**

- Exports `__all__`: `CASEEngine`, `CASEState`, `CASEResult`, `TokenUsage`, `EngineConfig`, `GlobalGuardrails`, `CognitiveRegistry`, `ModelRouter`, `BaseLLMClient`, `MockLLMClient`, `create_llm_client`, `RetryPolicy`, `AuditLedger`, `create_ledger`, `ValidationEngine`, `LLMBackedJudge`, `CASEError`, `PIIDetector`, `PIIFinding`, `RegexPIIDetector`, `NoopPIIDetector`, `PIIRedactor`, `NoopRedactor`, `Metrics`, `NoopMetrics`, `InMemoryMetrics`, `ResultStore`, `create_result_store`, `ENGINE_NAME`, `SCHEMA_VERSION`, `__version__`


#### `case_engine.__main__.py` — 33 LOC

**Role:** ``python -m case_engine`` entry point (delegates to the CLI).

**Contents:**



#### `case_engine.cli.py` — 865 LOC

**Role:** Command-line interface for the C.A.S.E. engine.

**Contents:**

- Exports `__all__`: `main`
- **def** `_build_parser() -> argparse.ArgumentParser` — Create the top-level argument parser (pure construction, no I/O)
- **def** `_make_engine(args: argparse.Namespace) -> CASEEngine` — Build an engine from parsed CLI arguments
- **def** `_print_result(result, as_json: bool) -> None` — Print a :class:`CASEResult` humanly or as JSON
- **def** `_cmd_run(args: argparse.Namespace) -> int` — Implement ``case-engine run``
- **def** `_cmd_demo(args: argparse.Namespace) -> int` — Implement ``case-engine demo`` (fully offline)
- **def** `_cmd_models(args: argparse.Namespace) -> int` — Implement ``case-engine models``
- **def** `_cmd_show(args: argparse.Namespace) -> int` — Implement ``case-engine show``
- **def** `_cmd_verify(args: argparse.Namespace) -> int` — Implement ``case-engine verify``
- **def** `_cmd_config(_args: argparse.Namespace) -> int` — Implement ``case-engine config``
- **def** `_cmd_history(args: argparse.Namespace) -> int` — Implement ``case-engine history`` (G12 result store)
- **def** `_cmd_doctor(args: argparse.Namespace) -> int` — Implement ``case-engine doctor`` (G17)
- **def** `_cmd_init(args: argparse.Namespace) -> int` — Implement ``case-engine init`` (G17)
- **def** `_cmd_export(args: argparse.Namespace) -> int` — Implement ``case-engine export`` (G17)
- **def** `main(argv: Sequence[str] | None=None) -> int` — CLI entry point


#### `case_engine.config.py` — 300 LOC

**Role:** Engine configuration: global guardrails + runtime options.

**Contents:**

- Exports `__all__`: `GlobalGuardrails`, `EngineConfig`
  - **class GlobalGuardrails** — Non-negotiable engine-wide safety rails (spec ``global_guardrails``)
    - method: `_warn_on_passthrough(cls, value: bool) -> bool` — Log a loud warning when unvalidated passthrough is enabled
  - **class EngineConfig** — Top-level runtime configuration for :class:`case_engine.engine.CASEEngine`
    - method: `from_file(cls, path: str | Path) -> EngineConfig` — Build a configuration from a JSON file
    - method: `load(cls, path: str | Path | None=None) -> EngineConfig` — Load configuration from *path*, the env var, or defaults


#### `case_engine.engine.py` — 1054 LOC

**Role:** CASEEngine — the top-level orchestrating facade.

**Contents:**

- Exports `__all__`: `CASEEngine`
  - **class CASEEngine** — Deterministic mental-model reasoning engine (the meta-agent constitution)
    - constructor: `__init__(self, config: EngineConfig | None=None, llm_client: Any | None=None, ledger: Any | None=None, judge: LLMJudge | None=None, registry: CognitiveRegistry | None=None, pii_detector: PIIDetector | None=None, audit_redactor: Any | None=None, metrics: Any | None=None, escalation_sink: Any | None=None, result_store: Any | None=None) -> None` — Resolve and wire all collaborators (constructor never touches the network)
    - method: `run(self, user_query: str, context: dict[str, Any] | None=None, domain_hint: str | None=None, forced_model_ids: list[str] | None=None, tee_attested: bool | None=None) -> CASEResult` — Run the full pipeline for one user query
    - method: `async run_async(self, user_query: str, context: dict[str, Any] | None=None, domain_hint: str | None=None, forced_model_ids: list[str] | None=None, tee_attested: bool | None=None) -> CASEResult` — Async twin of :meth:`run` — non-blocking event loop (G5, phase 1)
    - method: `verify_audit_chain(self) -> ChainVerification` — Verify the full hash chain of the audit ledger
    - method: `list_models(self) -> list[dict[str, Any]]` — Return a compact summary of every registered mental model
    - method: `describe_model(self, algorithm_id: str) -> dict[str, Any]` — Return the full registry entry for *algorithm_id*
    - method: `tool_spec(self, algorithm_id: str) -> dict[str, Any]` — Return the OpenAI-shaped tool spec for *algorithm_id*
    - method: `_audit_session_failed(self, session_id: str, exc: Exception) -> None` — Write a SESSION_FAILED event (best-effort; never masks the original)
    - method: `_escalation_result(self, state: CASEState | dict[str, Any], session_id: str, exc: Exception, ctx_count_before: int) -> CASEResult` — Build an escalation result after an infrastructure failure
    - method: `_result_from_state(self, state: CASEState | dict[str, Any], ctx_count_before: int) -> CASEResult` — Serialize final pipeline state into a :class:`CASEResult`
    - method: `_store_result(self, result: CASEResult) -> None` — Persist a completed result into the optional store (G12)
    - method: `resume(self, prior: Any, reviewer_verdict: str, reviewer_id: str='human-reviewer', approve: bool=True, context: dict[str, Any] | None=None, tee_attested: bool | None=None) -> CASEResult` — Re-run a session after a human verdict on an escalation (G6)
    - method: `_prior_evidence(prior: Any) -> tuple[str, str, dict[str, Any]]` — Extract ``(session_id, user_query, partial_traces)`` from *prior*


#### `case_engine.escalation.py` — 474 LOC

**Role:** Human-in-the-loop escalation: structured handoffs + pluggable sinks (G6).

**Contents:**

- Exports `__all__`: `EscalationDeliveryError`, `EscalationHandoff`, `EscalationSink`, `NoopEscalationSink`, `LoggingEscalationSink`, `FileEscalationSink`, `WebhookEscalationSink`, `handoff_from_result`
  - **class EscalationDeliveryError** — Raised when a *strict* escalation sink cannot deliver a handoff
  - **class EscalationHandoff** — Typed, self-contained ticket object produced by node_8 (G6)
  - **class EscalationSink** — Structural contract for escalation delivery targets (G6)
    - method: `deliver(self, handoff: EscalationHandoff) -> bool` — Deliver one handoff
  - **class NoopEscalationSink** — Default sink — records nothing, logs one DEBUG line
    - method: `deliver(self, handoff: EscalationHandoff) -> bool` — Log and discard the handoff (never raises)
  - **class LoggingEscalationSink** — Deliver escalations as structured WARNING log records
    - method: `deliver(self, handoff: EscalationHandoff) -> bool` — Emit one structured WARNING describing the handoff
  - **class FileEscalationSink** — Append-only JSONL delivery (one :class:`EscalationHandoff` per line)
    - constructor: `__init__(self, path: str | Path, strict: bool=False) -> None` — Store the target path (no I/O happens until first delivery)
    - method: `deliver(self, handoff: EscalationHandoff) -> bool` — Append the handoff as one JSON line (fsync'd)
  - **class WebhookEscalationSink** — HTTP POST delivery using the standard library (no extra dependency)
    - constructor: `__init__(self, url: str, timeout_s: float=5.0, headers: dict[str, str] | None=None, strict: bool=False) -> None` — Validate and store the webhook configuration (no I/O yet)
    - method: `timeout_s(self) -> float` — Return the configured per-delivery timeout in seconds
    - method: `deliver(self, handoff: EscalationHandoff) -> bool` — POST the handoff JSON to the configured endpoint
- **def** `handoff_from_result(result: Any, *, package: dict[str, Any] | None=None, reason: str='escalated', metadata: dict[str, Any] | None=None) -> EscalationHandoff` — Build an :class:`EscalationHandoff` from a ``CASEResult``


#### `case_engine.exceptions.py` — 348 LOC

**Role:** Exception hierarchy for the C.A.S.E. engine.

**Contents:**

- Exports `__all__`: `CASEError`, `ConfigurationError`, `RegistryError`, `SchemaViolationError`, `ToolCallRejectedError`, `RoutingError`, `LLMAdapterError`, `LLMResponseError`, `LLMProviderUnavailableError`, `PipelineError`, `TopologyViolationError`, `ReflexionExhaustedError`, `EscalationRequiredError`, `LedgerError`, `LedgerTamperError`, `StorageError`
  - **class CASEError** — Base class for every error intentionally raised by the engine
    - constructor: `__init__(self, message: str | None=None, **context: Any) -> None` — Initialise the error with an optional message and structured context
    - method: `__str__(self) -> str` — Render the message with the structured context appended
  - **class ConfigurationError** — Raised when engine configuration is missing, malformed or contradictory
  - **class RegistryError** — Raised when a mental-model / validation-rule lookup fails or the registry is inconsistent
  - **class RegistryPackError** — Raised when an external registry pack cannot be loaded or validated
  - **class SchemaViolationError** — Raised when a payload does not satisfy a mental-model Pydantic contract
  - **class ToolCallRejectedError** — Raised by the enforcement gate when an LLM tool call is rejected
  - **class RoutingError** — Raised when the Cynefin domain cannot be routed to mental models
  - **class LLMAdapterError** — Base class for LLM provider adapter failures (transport, auth, SDK missing)
  - **class LLMResponseError** — Raised when the provider response does not contain a usable tool call
  - **class LLMProviderUnavailableError** — Raised when an optional provider SDK is not installed or endpoint unreachable
  - **class PipelineError** — Raised when the execution pipeline itself fails (not a validation rejection)
  - **class TopologyViolationError** — Raised when a node attempts an illegal transition (graph topology guard)
  - **class ReflexionExhaustedError** — Raised when the reflexion loop consumed all retries without a valid trace
  - **class EscalationRequiredError** — Raised when the engine must hand the session to a human reviewer
  - **class LedgerError** — Raised when the audit ledger cannot append or be read
  - **class LedgerTamperError** — Raised when hash-chain verification detects tampering
  - **class StorageError** — Raised when the optional session-result store cannot operate (G12)


#### `case_engine.logging_setup.py` — 228 LOC

**Role:** Centralised structured logging for the C.A.S.E. engine.

**Contents:**

- Exports `__all__`: `get_logger`, `configure_logging`, `JsonFormatter`, `ConsoleFormatter`
- **def** `get_logger(name: str) -> logging.Logger` — Return a logger namespaced under the engine root logger
  - **class ConsoleFormatter** — Human-readable formatter: ``time LEVEL [logger] message (extras)``
    - method: `format(self, record: logging.LogRecord) -> str` — Render *record* as a compact console line
  - **class JsonFormatter** — One-JSON-object-per-line formatter for log shippers
    - method: `format(self, record: logging.LogRecord) -> str` — Render *record* as a JSON line
- **def** `configure_logging(level: str | int='INFO', json_mode: bool=False, log_file: str | None=None, stream: TextIO | None=None) -> logging.Logger` — Configure the engine root logger once, idempotently


#### `case_engine.messages.py` — 206 LOC

**Role:** Message-catalog seam for user-facing strings (G19, v2.3.0).

**Contents:**

- Exports `__all__`: `MessageCatalog`, `DictMessageCatalog`, `set_catalog`, `get_catalog`, `reset_catalog`, `msg`
  - **class MessageCatalog** — Structural contract for a translation catalog
    - method: `get(self, key: str) -> str | None` — Return the template for *key*, or ``None`` when untranslated
  - **class DictMessageCatalog** — Minimal dict-backed :class:`MessageCatalog`
    - constructor: `__init__(self, messages: dict[str, str]) -> None` — Store a shallow copy of *messages* (caller's dict is safe)
    - method: `get(self, key: str) -> str | None` — Return the template for *key* or ``None`` (protocol impl)
- **def** `set_catalog(catalog: MessageCatalog | None) -> None` — Install *catalog* as the process-wide message source
- **def** `get_catalog() -> MessageCatalog | None` — Return the active catalog (``None`` when using English defaults)
- **def** `reset_catalog() -> None` — Remove the active catalog (English defaults; used by tests)
- **def** `msg(key: str, default: str, **params: Any) -> str` — Resolve a user-facing message through the active catalog


#### `case_engine.observability.py` — 368 LOC

**Role:** Observability hooks: counters, timers and Prometheus text export (G11).

**Contents:**

- Exports `__all__`: `Metrics`, `NoopMetrics`, `InMemoryMetrics`, `safe_incr`, `safe_observe`, `render_prometheus`
  - **class Metrics** — Structural contract for a metrics sink (G11)
    - method: `incr(self, name: str, value: int=1, **labels: Any) -> None` — Add *value* to counter *name*
    - method: `observe(self, name: str, value: float, **labels: Any) -> None` — Record one observation (e.g. latency milliseconds) for *name*
    - method: `snapshot(self) -> dict[str, Any]` — Return a JSON-safe copy of the accumulated series
  - **class NoopMetrics** — Default metrics sink — every operation is a no-op
    - method: `incr(self, name: str, value: int=1, **labels: Any) -> None` — Drop the increment (no-op; never raises)
    - method: `observe(self, name: str, value: float, **labels: Any) -> None` — Drop the observation (no-op; never raises)
    - method: `snapshot(self) -> dict[str, Any]` — Return the empty snapshot
  - **class InMemoryMetrics** — Dict-backed metrics sink for tests, demos and single-process runs
    - constructor: `__init__(self) -> None` — Initialise empty series (no arguments; never raises)
    - method: `_key(name: str, labels: dict[str, Any]) -> str` — Build the stable ``name|k=v,k=v`` series key
    - method: `incr(self, name: str, value: int=1, **labels: Any) -> None` — Add *value* to the labelled counter
    - method: `observe(self, name: str, value: float, **labels: Any) -> None` — Record one observation for the labelled series
    - method: `snapshot(self) -> dict[str, Any]` — Return a deep copy of all series
- **def** `safe_incr(metrics: Any, name: str, value: int=1, **labels: Any) -> None` — Fail-open counter increment (never raises into the pipeline)
- **def** `safe_observe(metrics: Any, name: str, value: float, **labels: Any) -> None` — Fail-open observation (never raises into the pipeline)
- **def** `_series_from_key(key: str) -> tuple[str, dict[str, str]]` — Split an :class:`InMemoryMetrics` series key back into name+labels
- **def** `_escape_label(value: str) -> str` — Escape a label value for the Prometheus text format
- **def** `render_prometheus(metrics: InMemoryMetrics) -> str` — Render an :class:`InMemoryMetrics` snapshot as Prometheus text


#### `case_engine.privacy.py` — 445 LOC

**Role:** Privacy primitives: PII detection (G1) and deterministic redaction (G4).

**Contents:**

- Exports `__all__`: `PIICategory`, `PIIFinding`, `PIIDetector`, `RegexPIIDetector`, `NoopPIIDetector`, `PIIRedactor`, `NoopRedactor`
  - **class PIICategory** — PII kinds recognised by the shipped regex detector
  - **class PIIFinding** — Result of scanning one text for PII
  - **class PIIDetector** — Structural protocol any PII detector must satisfy
    - method: `detect(self, text: str) -> PIIFinding` — Scan *text* and report PII categories found
  - **class RegexPIIDetector** — Fully-offline, conservative regex detector for common PII kinds
    - constructor: `__init__(self) -> None` — Compile the pattern set (no I/O; safe to build per engine)
    - method: `_luhn_ok(candidate: str) -> bool` — Return ``True`` when *candidate* passes the Luhn checksum
    - method: `detect(self, text: str) -> PIIFinding` — Scan *text* for every category; never raises on bad input
  - **class NoopPIIDetector** — Always-negative detector (explicit deployment opt-out)
    - method: `detect(self, text: str) -> PIIFinding` — Return a negative finding unconditionally
  - **class PIIRedactor** — Deterministic, session-scoped tokenizing redactor for audit payloads
    - constructor: `__init__(self, detector: RegexPIIDetector | None=None) -> None` — Build the redactor around the given (or a fresh) detector
    - method: `redact_text(self, text: str) -> tuple[str, int]` — Tokenize every PII match in *text*
    - method: `redact_payload(self, payload: Any) -> tuple[Any, int]` — Recursively redact every string in a JSON-safe payload
    - method: `_token_for(self, value: str) -> str` — Return the stable token for *value*, assigning one if new
  - **class NoopRedactor** — Identity redactor — the behavior-neutral default
    - method: `redact_payload(self, payload: Any) -> tuple[Any, int]` — Return the payload untouched with a zero replacement count


#### `case_engine.server.py` — 279 LOC

**Role:** Optional FastAPI serving layer (G16, v2.3.0) — ``case_engine[server]``.

**Contents:**

- Exports `__all__`: `create_app`
  - **class _SessionMirror** — Thread-safe, bounded ``session_id -> CASEResult`` map (FIFO evict)
    - constructor: `__init__(self, capacity: int=_MIRROR_CAPACITY) -> None` — Create an empty mirror with the given capacity
    - method: `put(self, result: CASEResult) -> None` — Store *result*, evicting the oldest entry when full
    - method: `get(self, session_id: str) -> CASEResult | None` — Return the result for *session_id* or ``None``
  - **class RunRequest** — Request body for ``POST /sessions`` (typed, ``extra=forbid``)
- **def** `create_app(engine: CASEEngine | None=None, *, mirror_capacity: int=_MIRROR_CAPACITY) -> Any` — Build the FastAPI application around a C.A.S.E. engine
- **def** `_uvicorn_entry() -> None` — Run a local dev server (``python -m case_engine.server``)


#### `case_engine.storage.py` — 393 LOC

**Role:** First-class session-result persistence (G12).

**Contents:**

- Exports `__all__`: `ResultStore`, `InMemoryResultStore`, `SQLiteResultStore`, `create_result_store`
  - **class ResultStore** — Structural contract for session-result stores (G12)
    - method: `save(self, result: CASEResult) -> bool` — Persist one result (idempotent per ``session_id``)
    - method: `get(self, session_id: str) -> CASEResult | None` — Fetch one result by session id
    - method: `iter_since(self, since_iso: str | None=None, limit: int | None=None) -> Iterator[CASEResult]` — Yield stored results newest-first, optionally filtered
    - method: `close(self) -> None` — Release backend resources (idempotent)
  - **class InMemoryResultStore** — Dict-backed store for tests, demos and single-process tools
    - constructor: `__init__(self) -> None` — Initialise an empty store (never raises)
    - method: `save(self, result: CASEResult) -> bool` — Store the result keyed by ``session_id`` (never raises)
    - method: `get(self, session_id: str) -> CASEResult | None` — Return the stored result or ``None``
    - method: `iter_since(self, since_iso: str | None=None, limit: int | None=None) -> Iterator[CASEResult]` — Yield results newest-first by ``started_at``
    - method: `close(self) -> None` — Clear the store (idempotent; never raises)
  - **class SQLiteResultStore** — File-backed result store (stdlib ``sqlite3``, WAL-friendly)
    - constructor: `__init__(self, path: str | Path) -> None` — Open (or create) the results database
    - method: `save(self, result: CASEResult) -> bool` — Upsert the result row (best-effort; never raises)
    - method: `get(self, session_id: str) -> CASEResult | None` — Fetch one result by id (``None`` when absent)
    - method: `iter_since(self, since_iso: str | None=None, limit: int | None=None) -> Iterator[CASEResult]` — Yield results newest-first (SQL-backed projection)
    - method: `close(self) -> None` — Close the connection (idempotent; never raises)
- **def** `create_result_store(backend: str, path: str | None=None) -> ResultStore | None` — Instantiate a result store by key (G12 factory)


#### `case_engine.version.py` — 46 LOC

**Role:** Version and identity constants for the C.A.S.E. engine.

**Contents:**



---

## 2. Schema layer — typed contracts (Pydantic V2)

Pure data contracts, no I/O. `schemas/mental_models.py` defines the 15 typed trace classes whose *class names are the LLM tool names* (Strategy 1 of the spec); `schemas/pipeline.py` carries the mutable `CASEState` between nodes and the final `CASEResult`; `schemas/reports.py` holds the validation evidence objects `GateDecision`, `RuleReport`, `TraceValidationReport` that feed both reflexion feedback and the audit ledger; `schemas/base.py` anchors every trace to `BaseTrace` (session, timestamps, provenance).


#### `case_engine.schemas.py` — 82 LOC

**Role:** Schemas subpackage: mental-model tool contracts + pipeline/report models.

**Contents:**

- Exports `__all__`: `BaseTrace`, `utc_now_iso`, `SCHEMA_BY_MODEL_ID`, `OODAExecutionTrace`, `FirstPrinciplesDeconstruction`, `SixHatsEvaluation`, `ParetoOptimizationPlan`, `RootCauseAnalysisTrace`, `OccamHypothesisRanking`, `EmotionalRegulationFilter`, `EvolutionaryArchitecturePlan`, `InversionPreMortem`, `LegacyRefactoringAudit`, `ConsequenceMappingTree`, `SociotechnicalAlignmentCheck`, `ComplexityDomainClassifier`, `MetricGamingPrevention`, `TelemetryDriftValidation`, `CASEState`, `CASEResult`, `AuditEventType`, `CynefinDomain`, `PipelinePhase`, `SpecialistDomain`, `GateDecision`, `RuleReport`, `TraceRecord`, `TraceValidationReport`


#### `case_engine.schemas.base.py` — 105 LOC

**Role:** Base contract shared by every mental-model trace schema.

**Contents:**

- **def** `utc_now_iso() -> str` — Return the current UTC time as an ISO-8601 string with offset
  - **class BaseTrace** — Common foundation for all mental-model execution traces


#### `case_engine.schemas.mental_models.py` — 540 LOC

**Role:** The 15 authoritative mental-model Pydantic contracts (MM001–MM015).

**Contents:**

- Exports `__all__`: `OODAExecutionTrace`, `FirstPrinciplesDeconstruction`, `SixHatsEvaluation`, `ParetoOptimizationPlan`, `RootCauseAnalysisTrace`, `OccamHypothesisRanking`, `EmotionalRegulationFilter`, `EvolutionaryArchitecturePlan`, `InversionPreMortem`, `LegacyRefactoringAudit`, `ConsequenceMappingTree`, `SociotechnicalAlignmentCheck`, `ComplexityDomainClassifier`, `MetricGamingPrevention`, `TelemetryDriftValidation`, `SCHEMA_BY_MODEL_ID`
  - **class OODAExecutionTrace** — MM001 — OODA Loop execution trace (Observe → Orient → Decide → Act)
  - **class FirstPrinciplesDeconstruction** — MM002 — First Principles deconstruction trace
  - **class SixHatsEvaluation** — MM003 — Six Thinking Hats evaluation trace
  - **class ParetoOptimizationPlan** — MM004 — Pareto 80/20 optimization plan trace
  - **class RootCauseAnalysisTrace** — MM005 — Root Cause Analysis (5 Whys) trace
  - **class OccamHypothesisRanking** — MM006 — Occam's Razor hypothesis ranking trace
  - **class EmotionalRegulationFilter** — MM007 — 5x5 Rule emotional-regulation trace
  - **class EvolutionaryArchitecturePlan** — MM008 — Gall's Law evolutionary architecture plan
  - **class InversionPreMortem** — MM009 — Inversion pre-mortem trace
  - **class LegacyRefactoringAudit** — MM010 — Chesterton's Fence legacy-refactoring audit
  - **class ConsequenceMappingTree** — MM011 — Second-Order Thinking consequence tree
  - **class SociotechnicalAlignmentCheck** — MM012 — Conway's Law sociotechnical alignment check
  - **class ComplexityDomainClassifier** — MM013 — Cynefin complexity-domain classification (pipeline node_1)
  - **class MetricGamingPrevention** — MM014 — Goodhart's Law metric-gaming prevention
  - **class TelemetryDriftValidation** — MM015 — Map is Not the Territory telemetry-drift validation


#### `case_engine.schemas.pipeline.py` — 433 LOC

**Role:** Pipeline state and result contracts.

**Contents:**

- Exports `__all__`: `CynefinDomain`, `SpecialistDomain`, `PipelinePhase`, `AuditEventType`, `TokenUsage`, `CASEState`, `CASEResult`
  - **class CynefinDomain** — The four canonical Cynefin domains (spec node_1 output)
  - **class SpecialistDomain** — Specialist routing domains supported by the spec routing matrix
  - **class PipelinePhase** — Human-readable phase labels attached to the audit trail
  - **class AuditEventType** — Event types written to the hash-chained audit ledger
  - **class TokenUsage** — Aggregate LLM token accounting for one session (G7)
    - method: `add(self, prompt_tokens: int, completion_tokens: int) -> None` — Accumulate one provider response's usage into this aggregate
  - **class CASEState** — Mutable pipeline state passed node-to-node (JSON-safe values only)
- **def** `_utcnow() -> str` — Return the current UTC time as an ISO-8601 string with offset
  - **class CASEResult** — Final, user-facing outcome of one ``CASEEngine.run`` invocation


#### `case_engine.schemas.reports.py` — 218 LOC

**Role:** Report models shared by validation, enforcement and the pipeline.

**Contents:**

- Exports `__all__`: `GateDecision`, `RuleReport`, `TraceValidationReport`, `TraceRecord`
  - **class GateDecision** — Outcome of the enforcement gate for one tool call (Strategy 1)
  - **class RuleReport** — Result of executing a single validation rule against a trace
  - **class TraceValidationReport** — Full validation verdict for one generated trace
    - method: `failed_rules(self) -> list[str]` — Return the rule ids that failed (empty list when none)
  - **class TraceRecord** — A generated (possibly re-generated) trace retained in pipeline state


---

## 3. Registry layer — the constitution data & its lookups

Static, frozen knowledge: 15 mental models (MM001–MM015) with golden examples, 16 validation rules (VR001–VR016) with bindings, and the Cynefin routing matrix. `registry/models.py` loads and consistency-checks this pack (`_check_consistency` runs at construction — a registry that fails self-checks can never serve a session); `registry/routing.py` implements deterministic, tie-broken model selection. Data lives in `model_data_a.py`, `model_data_b.py`, `rule_data.py`; `registry/pack.py` (v2.3.0, G15) adds the external JSON pack contract — `load_registry_pack` / `dump_registry_pack` plus the `CognitiveRegistry.load_pack(overlay=)` / `.dump_pack` surface, all validated by the same entry models and consistency checker.


#### `case_engine.registry.py` — 59 LOC

**Role:** Cognitive registry subpackage: model/rule definitions + Cynefin router.

**Contents:**

- Exports `__all__`: `CognitiveRegistry`, `MentalModelDefinition`, `ValidationRuleDefinition`, `ModelRouter`, `PACK_TOP_LEVEL_KEYS`, `dump_registry_pack`, `load_registry_pack`


#### `case_engine.registry.model_data_a.py` — 473 LOC

**Role:** Cognitive registry data — mental models MM001–MM008 (verbatim from spec).

**Contents:**



#### `case_engine.registry.model_data_b.py` — 445 LOC

**Role:** Cognitive registry data — mental models MM009–MM015 (verbatim from spec).

**Contents:**



#### `case_engine.registry.models.py` — 548 LOC

**Role:** Typed registry primitives: mental-model and validation-rule definitions.

**Contents:**

- Exports `__all__`: `MentalModelDefinition`, `ValidationRuleDefinition`, `CognitiveRegistry`
  - **class MentalModelDefinition** — One entry of the cognitive registry (one mental model)
    - method: `tool_name(self) -> str` — Return the LLM tool name for this model (its schema class name)
  - **class ValidationRuleDefinition** — One entry of the validation-rules registry
  - **class CognitiveRegistry** — Immutable, consistency-checked view over the cognitive registry
    - constructor: `__init__(self, models: list[MentalModelDefinition], rules: list[ValidationRuleDefinition], routing_matrix: dict[str, list[str]]) -> None` — Store and validate the registry contents
    - method: `load_default(cls) -> CognitiveRegistry` — Build the registry from the spec-transcribed default data
    - method: `load_pack(cls, path: Path | str, *, overlay: bool=False) -> CognitiveRegistry` — Build the registry from an external JSON pack (G15)
    - method: `dump_pack(self, path: Path | str | None=None) -> dict[str, Any]` — Export this registry as a JSON pack document (G15)
    - method: `_check_consistency(self) -> None` — Cross-check models, rules and routing matrix
    - method: `get(self, algorithm_id: str) -> MentalModelDefinition` — Return the model definition for *algorithm_id*
    - method: `get_by_tool_name(self, tool_name: str) -> MentalModelDefinition` — Return the model whose schema class (tool name) is *tool_name*
    - method: `get_rule(self, rule_id: str) -> ValidationRuleDefinition` — Return the rule definition for *rule_id*
    - method: `all_models(self) -> list[MentalModelDefinition]` — Return all model definitions ordered by algorithm id
    - method: `all_rules(self) -> list[ValidationRuleDefinition]` — Return all rule definitions ordered by rule id
    - method: `routing_matrix(self) -> dict[str, list[str]]` — Return a defensive copy of the routing matrix
    - method: `__len__(self) -> int` — Return the number of registered mental models


#### `case_engine.registry.pack.py` — 291 LOC

**Role:** External JSON registry packs (G15, v2.3.0).

**Contents:**

- Exports `__all__`: `PACK_TOP_LEVEL_KEYS`, `dump_registry_pack`, `load_registry_pack`
- **def** `dump_registry_pack(registry: CognitiveRegistry, path: str | Path | None=None) -> dict[str, Any]` — Project a registry into the JSON pack document shape
- **def** `load_registry_pack(path: str | Path) -> tuple[list[MentalModelDefinition], list[ValidationRuleDefinition], dict[str, list[str]]]` — Read and validate a JSON registry pack (fail-closed)
- **def** `_load_pack(cls: type[CognitiveRegistry], path: str | Path, *, overlay: bool=False) -> CognitiveRegistry` — Implementation of :meth:`CognitiveRegistry.load_pack`


#### `case_engine.registry.routing.py` — 198 LOC

**Role:** Cynefin-domain model router (pipeline node_2).

**Contents:**

- Exports `__all__`: `ModelRouter`
  - **class ModelRouter** — Rank-and-select mental models for a classified domain
    - constructor: `__init__(self, registry: CognitiveRegistry) -> None` — Bind the router to a registry
    - method: `select(self, domain: str, top_n: int=3, forced_model_ids: list[str] | None=None) -> list[MentalModelDefinition]` — Select the mental models to execute for *domain*
    - method: `explain(self, domain: str) -> dict[str, float]` — Return the affinity score of every routed model for *domain*


#### `case_engine.registry.rule_data.py` — 260 LOC

**Role:** Cognitive registry data — validation rules VR001–VR016 and routing matrix.

**Contents:**



---

## 4. Enforcement layer — zero-trust tool-call gating

The security core around every LLM interaction. `enforcement/gate.py` (`SchemaGate`) re-validates *every* tool call zero-trust with `extra=forbid` — it never trusts prior validation and treats unknown tool names as enforcement faults; `enforcement/tool_factory.py` generates OpenAI/Anthropic JSON-schema tool specs directly from the typed Pydantic classes and never `exec()`s spec strings.


#### `case_engine.enforcement.py` — 44 LOC

**Role:** Enforcement subpackage: the Pydantic gate + provider tool-spec factory.

**Contents:**

- Exports `__all__`: `SchemaGate`, `ToolFactory`


#### `case_engine.enforcement.gate.py` — 190 LOC

**Role:** The enforcement gate: every tool call is re-validated server-side.

**Contents:**

- Exports `__all__`: `SchemaGate`
  - **class SchemaGate** — Validate LLM tool-call payloads against mental-model contracts
    - constructor: `__init__(self, registry: CognitiveRegistry) -> None` — Bind the gate to the cognitive registry
    - method: `check(self, tool_name: str, arguments: dict) -> GateDecision` — Validate *arguments* against the contract registered as *tool_name*


#### `case_engine.enforcement.tool_factory.py` — 221 LOC

**Role:** Tool-spec factory: turns registry contracts into provider tool payloads.

**Contents:**

- Exports `__all__`: `ToolFactory`
  - **class ToolFactory** — Build OpenAI- and Anthropic-style tool specs from registry entries
    - constructor: `__init__(self) -> None` — Create the factory (stateless; kept as a class for extensibility)
    - method: `_schema_class(self, model: MentalModelDefinition) -> type[BaseTrace]` — Resolve the contract class for *model*
    - method: `json_schema(self, model: MentalModelDefinition) -> dict[str, Any]` — Return the JSON Schema for *model*'s contract
    - method: `openai_tool(self, model: MentalModelDefinition) -> dict[str, Any]` — Build a Chat-Completions tool spec for *model*
    - method: `anthropic_tool(self, model: MentalModelDefinition) -> dict[str, Any]` — Build an Anthropic Messages-API tool spec for *model*
    - method: `generic_tool(self, model: MentalModelDefinition) -> dict[str, Any]` — Build the canonical (OpenAI-shaped) spec used inside pipeline state


---

## 5. Validation layer — 16 deterministic rules + optional LLM judge

Deterministic, code-only verdicts: 16 rule functions evaluate every generated trace (schema validity, semantic coverage, constraint budgets, universal trace-integrity VR016). The optional `LLMBackedJudge` adds semantic judgment for VR001/002/005/012/014 with a strictly *veto-only* contract — it can reject, never rescue — and degrades fail-open to heuristic-only when its own LLM fails (the judge is optional hardening; the deterministic rules are not).


#### `case_engine.validation.py` — 46 LOC

**Role:** Validation subpackage: rules, judge, and the ValidationEngine (node_5).

**Contents:**

- Exports `__all__`: `ValidationEngine`, `LLMBackedJudge`, `LLMJudge`, `JudgeVerdict`, `VALIDATORS`, `run_rule`


#### `case_engine.validation.engine.py` — 247 LOC

**Role:** ValidationEngine: schema gate + rule battery for one generated trace.

**Contents:**

- Exports `__all__`: `ValidationEngine`
  - **class ValidationEngine** — Validate traces against schemas and the rule battery
    - constructor: `__init__(self, registry: CognitiveRegistry, guardrails: GlobalGuardrails, judge: LLMJudge | None=None) -> None` — Bind the validator to registry, guardrails and optional judge
    - method: `validate(self, model_def: MentalModelDefinition, payload: dict[str, Any]) -> TraceValidationReport` — Validate *payload* for *model_def* end-to-end
    - method: `_finish(self, model_id: str, model_def: MentalModelDefinition, schema_valid: bool, schema_errors: list[str], rule_reports: list, started: float) -> TraceValidationReport` — Assemble the report, enforce the latency guardrail, log the verdict


#### `case_engine.validation.llm_judge.py` — 463 LOC

**Role:** Optional LLM-as-Judge upgrade for semantic validation rules.

**Contents:**

- Exports `__all__`: `JudgeVerdict`, `LLMJudge`, `LLMBackedJudge`, `SEMANTIC_RULE_IDS`
- **def** `_short(text: Any, limit: int=160) -> str` — Truncate *text* for inclusion in structured log extras
  - **class _DeadlineExceeded** — Internal: the judge's audit call outlived its hard deadline (G3)
  - **class JudgeVerdict** — Verdict returned by a judge for one rule application
  - **class LLMJudge** — Structural protocol any judge implementation must satisfy
    - method: `audit(self, auditor_prompt: str, payload: dict[str, Any]) -> JudgeVerdict` — Audit *payload* against *auditor_prompt* and return a verdict
  - **class LLMBackedJudge** — Judge implementation that audits via any :class:`BaseLLMClient`
    - constructor: `__init__(self, llm_client: Any, enabled: bool=True, timeout_ms: float | None=1000.0) -> None` — Store the client, the switch and the audit deadline
    - method: `for_guardrails(cls, llm_client: Any, guardrails: Any, enabled: bool=True) -> LLMBackedJudge` — Build a judge whose deadline is derived from the validation budget (G13)
    - method: `_call_with_deadline(self, request: ToolCallRequest) -> ToolCallResult` — Run the client call under the configured hard deadline (G3)
    - method: `audit(self, auditor_prompt: str, payload: dict[str, Any]) -> JudgeVerdict` — Run one adversarial audit turn against the configured client


#### `case_engine.validation.rules.py` — 1205 LOC

**Role:** The 16 deterministic validation-rule implementations (VR001–VR016).

**Contents:**

- Exports `__all__`: `run_rule`, `VALIDATORS`, `content_tokens`
- **def** `_short(text: Any, limit: int=_LOG_LIMIT) -> str` — Truncate *text* for inclusion in structured log extras
- **def** `_tokens(text: str) -> set[str]` — Tokenize *text* into a normalized term set
- **def** `content_tokens(text: str) -> set[str]` — Return content-bearing tokens (stopwords and 1-char terms removed)
- **def** `_shared_content(text_a: str, text_b: str) -> set[str]` — Return the shared content tokens between two texts (plural-tolerant)
- **def** `_vr001(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR001: ``orient_context`` must reference >=1 ``observe_raw_data`` item
- **def** `_vr002(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR002: ``fundamental_truths`` must be analogy-free
- **def** `_vr003(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR003: ``len(black_hat_risks) <= len(green_hat_creativity)``
- **def** `_vr004(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR004: ``len(vital_few_20_percent) < len(trivial_many_80_percent)``
- **def** `_vr005(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR005: root cause must be systemic, never a person
- **def** `_vr006(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR006: ``simplest_viable_answer`` must match ``argmin(assumptions)``
- **def** `_vr007(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR007: if it won't matter in 5 years, budget <= 5 minutes
- **def** `_vr008(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR008: ``simple_v1_working_system`` describes <=3 components
- **def** `_vr009(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR009: ``len(preemptive_safeguards) == len(critical_failure_vectors)``
- **def** `_vr010(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR010: ``characterization_tests_required`` must be non-empty
- **def** `_vr011(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR011: all three consequence tiers must contain >=2 items
- **def** `_vr012(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR012: ``proposed_team_topology`` must use boundary language
- **def** `_vr013(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR013: ``selected_methodology`` must fit ``assigned_domain``
- **def** `_vr014(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR014: counter metric must engage with the gaming vector
- **def** `_vr015(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR015: ``len(observability_requirements) >= len(expected_drift_vectors)``
- **def** `_vr016(trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails) -> tuple[bool, str]` — VR016: ``trace.confidence >= model.confidence_threshold``
- **def** `_dump_trace(trace: Any) -> dict[str, Any]` — Serialize a trace for the judge payload (Pydantic or duck-typed)
- **def** `run_rule(rule_id: str, trace: Any, model_def: MentalModelDefinition | None, guardrails: GlobalGuardrails, judge: LLMJudge | None=None) -> RuleReport` — Execute one validation rule with judge upgrade and fail-closed safety


---

## 6. LLM layer — provider-agnostic adapters behind one protocol

Every adapter implements the identical `BaseLLMClient` tool-call protocol; payloads are provider-shaped but semantically identical. `mock_client.py` is a deterministic offline client that replays registry gold examples and can inject failures (`fail_next`, hallucinated tools, malformed payloads) — it is what makes the test suite run in under a second with zero network. The OpenAI, Anthropic, Ollama and vLLM adapters lazy-import their SDKs and normalize every provider error into the engine exception hierarchy. `retry.py` (v2.2.0, G8) adds the shared provider-resilience layer — validated exponential-backoff policy with jitter and `Retry-After` support, a transport-failure classifier and sync/async retry drivers — wired into the network adapters so exhausted budgets surface as `LLMProviderUnavailableError`. Every adapter also exposes the async twin `generate_tool_call_async` (v2.2.0, G5).


#### `case_engine.llm.py` — 105 LOC

**Role:** LLM subpackage: provider-agnostic adapters + the client factory.

**Contents:**

- Exports `__all__`: `BaseLLMClient`, `ToolCallRequest`, `ToolCallResult`, `MockLLMClient`, `OpenAIClient`, `OpenAICompatibleClient`, `AnthropicClient`, `OllamaClient`, `VLLMClient`, `create_llm_client`
- **def** `create_llm_client(provider: str, **kwargs: object) -> BaseLLMClient` — Instantiate a provider adapter by key


#### `case_engine.llm.anthropic_client.py` — 346 LOC

**Role:** Anthropic Messages-API adapter (forced tool use).

**Contents:**

- Exports `__all__`: `AnthropicClient`
  - **class AnthropicClient** — Adapter for the Anthropic Messages API
    - constructor: `__init__(self, model: str='claude-sonnet-4-20250514', api_key: str | None=None, max_tokens: int=4096, temperature: float=0.0, timeout_s: float=60.0, retry_attempts: int=3, retry_initial_backoff_s: float=0.5, retry_max_backoff_s: float=8.0, client: Any | None=None) -> None` — Store settings; lazily construct the SDK client when not injected
    - method: `generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult` — Force one schema-gated tool call via the Messages API
    - method: `_convert_tools(openai_specs: list[dict[str, Any]]) -> list[dict[str, Any]]` — Convert OpenAI-shaped specs to Anthropic tool dicts
    - method: `build_request_kwargs(request: ToolCallRequest, model: str='claude-sonnet-4-20250514', max_tokens: int=4096, temperature: float=0.0) -> dict[str, Any]` — Build Messages-API kwargs (pure; unit-testable without SDK)


#### `case_engine.llm.base.py` — 312 LOC

**Role:** LLM adapter base contracts — the provider-agnostic tool-call protocol.

**Contents:**

- Exports `__all__`: `ToolCallRequest`, `ToolCallResult`, `BaseLLMClient`
  - **class ToolCallRequest** — One schema-gated generation request
    - method: `primary_tool_name(self) -> str` — Return the name of the first (usually only) requested tool
  - **class ToolCallResult** — One tool call extracted from a provider response
  - **class BaseLLMClient** — Abstract provider adapter — the contract every backend honours
    - method: `generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult` — Force one schema-gated tool call
    - method: `async generate_tool_call_async(self, request: ToolCallRequest) -> ToolCallResult` — Async twin of :meth:`generate_tool_call` (G5, phase 1)
    - method: `healthcheck(self) -> bool` — Cheap reachability probe; adapters may override
    - method: `_timed(self, request: ToolCallRequest) -> float` — Return elapsed seconds since ``request`` dispatch started
    - method: `_now() -> float` — Return a monotonic timestamp for latency measurement


#### `case_engine.llm.mock_client.py` — 266 LOC

**Role:** Deterministic MockLLMClient — offline demos and reproducible tests.

**Contents:**

- Exports `__all__`: `MockLLMClient`
  - **class MockLLMClient** — Scripted, deterministic stand-in for any provider
    - constructor: `__init__(self, script: dict[str, dict[str, Any]] | None=None, default_tool: str | None=None, failure_plan: list[str] | None=None) -> None` — Create a mock client
    - method: `from_registry_examples(cls, registry: CognitiveRegistry) -> MockLLMClient` — Build a client scripted with every registry model's gold example
    - method: `fail_next(self, times: int=1, mode: str='invalid_args') -> MockLLMClient` — Queue deterministic failures for the next *times* calls
    - method: `generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult` — Serve the scripted payload for the requested tool
    - method: `describe_script(self) -> str` — Return a pretty JSON dump of the script (debugging helper)


#### `case_engine.llm.ollama_client.py` — 148 LOC

**Role:** Local-model adapters: Ollama and vLLM (both OpenAI-compatible).

**Contents:**

- Exports `__all__`: `OllamaClient`, `VLLMClient`
  - **class OllamaClient** — Adapter for Ollama's OpenAI-compatible endpoint
    - constructor: `__init__(self, model: str='llama3.1:8b', base_url: str='http://localhost:11434/v1', temperature: float=0.0, timeout_s: float=120.0, client: object | None=None) -> None` — Point the OpenAI-compatible adapter at the local Ollama daemon
  - **class VLLMClient** — Adapter for a vLLM OpenAI server
    - constructor: `__init__(self, base_url: str, model: str, api_key: str='vllm', temperature: float=0.0, timeout_s: float=120.0, client: object | None=None) -> None` — Point the OpenAI-compatible adapter at a vLLM server


#### `case_engine.llm.openai_client.py` — 373 LOC

**Role:** OpenAI Chat-Completions adapter (tool_choice='required' enforcement).

**Contents:**

- Exports `__all__`: `OpenAIClient`, `OpenAICompatibleClient`
  - **class OpenAIClient** — Adapter for the official OpenAI Chat-Completions API
    - constructor: `__init__(self, model: str='gpt-4o', api_key: str | None=None, base_url: str | None=None, temperature: float=0.0, timeout_s: float=60.0, retry_attempts: int=3, retry_initial_backoff_s: float=0.5, retry_max_backoff_s: float=8.0, client: Any | None=None) -> None` — Store settings; lazily construct the SDK client when not injected
    - method: `generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult` — Force one schema-gated tool call via Chat-Completions
    - method: `build_request_kwargs(request: ToolCallRequest, model: str='gpt-4o', temperature: float=0.0) -> dict[str, Any]` — Build the Chat-Completions kwargs (pure; unit-testable without SDK)
  - **class OpenAICompatibleClient** — Adapter for any OpenAI-compatible endpoint (vLLM, LM Studio, gateways)
    - constructor: `__init__(self, base_url: str, model: str, api_key: str | None='local', temperature: float=0.0, timeout_s: float=120.0, retry_attempts: int=3, retry_initial_backoff_s: float=0.5, retry_max_backoff_s: float=8.0, client: Any | None=None) -> None` — Delegate to :class:`OpenAIClient` with a custom endpoint


#### `case_engine.llm.retry.py` — 337 LOC

**Role:** Provider-resilience layer: exponential backoff with jitter (G8).

**Contents:**

- Exports `__all__`: `RetryPolicy`, `classify_transport_failure`, `retry_after_seconds`, `call_with_retries`, `acall_with_retries`
  - **class RetryPolicy** — Immutable retry parameters for one adapter
    - method: `sleep_for(self, attempt: int, retry_after: float | None=None) -> float` — Return the sleep seconds before retry *attempt*
- **def** `classify_transport_failure(exc: BaseException, retryable_status_codes: frozenset[int]=RETRYABLE_STATUS_CODES) -> str | None` — Classify an SDK exception as retryable transport failure or not
- **def** `retry_after_seconds(exc: BaseException) -> float | None` — Extract a numeric ``Retry-After`` hint from an SDK exception
- **def** `call_with_retries(fn: Callable[[], T], *, policy: RetryPolicy, provider: str, what: str) -> T` — Execute *fn* with the policy's retry loop (synchronous)
- **def** `async acall_with_retries(fn: Callable[[], Any], *, policy: RetryPolicy, provider: str, what: str) -> Any` — Async twin of :func:`call_with_retries` (awaits sleep, not threads)


---

## 7. Audit layer — tamper-evident hash-chained ledger

Every pipeline step appends an `AuditEvent` (seq, type, node, payload digest, prev-hash, own SHA-256). `audit/base.py` defines the event model (incl. the `schema_version` field, v2.2.0 G10), the canonical JSON recipe, chain math and `verify_chain`; three interchangeable backends implement it: `jsonl_ledger.py` (fsync'd, resumable, default; cross-process `flock` + tail re-read per append — G9 — and size-based rotation into `.segN` segments with auto-anchoring — G10), `sqlite_ledger.py` (SQL-queryable), `memory_ledger.py` (tests). `audit/anchor.py` (v2.2.0, G10) externalizes tamper evidence: `AnchorSink` implementations (`FileAnchorSink`, `MemoryAnchorSink`) receive signed anchor records built by `make_anchor_record` so a chain can be re-verified even if its head is ever disputed. Appends are fail-closed: a ledger write failure halts the session rather than allowing an unaudited step.


#### `case_engine.audit.py` — 135 LOC

**Role:** Audit subpackage: hash-chained ledger backends + factory.

**Contents:**

- Exports `__all__`: `AuditLedger`, `AuditEvent`, `ChainVerification`, `JSONLLedger`, `SQLiteLedger`, `InMemoryLedger`, `create_ledger`, `compute_event_hash`, `canonical_json`, `GENESIS_HASH`, `PayloadCipher`, `AEADPayloadCipher`, `AnchorSink`, `FileAnchorSink`, `MemoryAnchorSink`, `make_anchor_record`, `AUDIT_EVENT_SCHEMA_VERSION`
- **def** `create_ledger(backend: str, path: str | None=None, payload_cipher: Any | None=None) -> AuditLedger` — Instantiate a ledger backend by key


#### `case_engine.audit.anchor.py` — 199 LOC

**Role:** External head-hash anchoring for audit ledgers (G10).

**Contents:**

- Exports `__all__`: `AnchorSink`, `FileAnchorSink`, `MemoryAnchorSink`, `make_anchor_record`
- **def** `make_anchor_record(head_hash: str, head_seq: int, source_path: str | Path, schema_version: int=1) -> dict[str, Any]` — Build one anchoring record (pure; shared by ledgers and sinks)
  - **class AnchorSink** — Structural contract for external anchor stores (G10)
    - method: `write(self, record: dict[str, Any]) -> None` — Persist one anchor record durably
  - **class FileAnchorSink** — Append-only JSONL anchor file (one record per line, fsync'd)
    - constructor: `__init__(self, path: str | Path) -> None` — Store the target path (no I/O until first write)
    - method: `path(self) -> Path` — Return the anchor file path (for operators and verifiers)
    - method: `write(self, record: dict[str, Any]) -> None` — Append one anchor record as a JSON line (fsync'd)
  - **class MemoryAnchorSink** — In-memory anchor sink for tests and dry-run verification
    - constructor: `__init__(self) -> None` — Initialise with an empty record list (never raises)
    - method: `write(self, record: dict[str, Any]) -> None` — Append the record to :attr:`records` (never raises)
- **def** `_dump_record(record: dict[str, Any]) -> str` — Serialize one anchor record deterministically (module helper)


#### `case_engine.audit.base.py` — 369 LOC

**Role:** Hash-chained audit ledger contracts — the tamper-evidence backbone.

**Contents:**

- Exports `__all__`: `GENESIS_HASH`, `AuditEvent`, `ChainVerification`, `AuditLedger`, `compute_event_hash`, `canonical_json`
- **def** `canonical_json(obj: Any) -> str` — Return the deterministic JSON serialization used for hashing
- **def** `compute_event_hash(prev_hash: str, seq: int, timestamp: str, event_type: str, session_id: str, node: str, payload: dict[str, Any]) -> str` — Compute the SHA-256 chain hash for one event
  - **class AuditEvent** — One hash-chained ledger entry
  - **class ChainVerification** — Result of a full-chain verification sweep
  - **class AuditLedger** — Abstract append-only, hash-chained audit backend
    - method: `append(self, event_type: str, session_id: str, node: str, payload: dict[str, Any]) -> AuditEvent` — Append one event and return the chained entry
    - method: `iter_events(self) -> Iterator[AuditEvent]` — Yield every stored event in chain order (seq ascending)
    - method: `close(self) -> None` — Release backend resources (idempotent)
    - method: `verify_chain(self) -> ChainVerification` — Re-derive the whole chain and report the first inconsistency
    - method: `count(self) -> int` — Return the number of stored events


#### `case_engine.audit.crypto.py` — 283 LOC

**Role:** Payload-at-rest encryption for audit ledgers (G4b, optional).

**Contents:**

- Exports `__all__`: `PayloadCipher`, `AEADPayloadCipher`, `load_key`, `KEY_ENV_VAR`
  - **class PayloadCipher** — Structural protocol for at-rest payload ciphers
    - method: `encrypt(self, payload: dict[str, Any]) -> dict[str, Any]` — Encrypt *payload* into an envelope dict
    - method: `decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]` — Decrypt an envelope dict produced by :meth:`encrypt`
- **def** `load_key(key: bytes | str | None, key_env: str=KEY_ENV_VAR) -> bytes` — Resolve a 32-byte AES key from explicit input or the environment
  - **class AEADPayloadCipher** — AES-256-GCM payload cipher (encrypt-then-chain at-rest protection)
    - constructor: `__init__(self, key: bytes | str | None=None, key_env: str=KEY_ENV_VAR) -> None` — Resolve the key and lazily bind the AESGCM primitive
    - method: `encrypt(self, payload: dict[str, Any]) -> dict[str, Any]` — Encrypt *payload* into a fresh-nonce AESGCM envelope
    - method: `decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]` — Decrypt an AESGCM envelope back into the original payload


#### `case_engine.audit.jsonl_ledger.py` — 852 LOC

**Role:** Append-only JSONL audit ledger (human-readable, grep-able, git-friendly).

**Contents:**

- Exports `__all__`: `JSONLLedger`
- **def** `_read_tail(path: Path) -> dict[str, Any] | None` — Return the last complete JSON record of *path* (module helper)
  - **class JSONLLedger** — File-backed hash-chained ledger (one JSON object per line)
    - constructor: `__init__(self, path: str | Path, fsync: bool=True, payload_cipher: Any | None=None, cross_process_safe: bool=True, max_bytes: int | None=None, anchor_sink: Any | None=None) -> None` — Open (or create) the ledger and replay any existing chain
    - method: `_segment_path(self, index: int) -> Path` — Return the file path for segment *index* (0 = active file)
    - method: `_discover_segments(self) -> None` — Set ``_segment_index`` to the highest existing rotated segment
    - method: `_replay(self) -> None` — Restore ``_seq``/``_last_hash`` from the active segment
    - method: `_maybe_rotate(self, incoming_bytes: int) -> None` — Rotate the active segment when the append would overflow it
    - method: `anchor(self, note: str | None=None) -> dict[str, Any] | None` — Commit the current chain head to the external sink (G10)
    - method: `append(self, event_type: str, session_id: str, node: str, payload: dict[str, Any]) -> AuditEvent` — Append one event under an exclusive lock
    - method: `_refresh_tail_under_lock(self) -> None` — Adopt an externally advanced chain tail (G9 helper)
    - method: `iter_events(self, decrypt: bool=False) -> Iterator[AuditEvent]` — Yield every event of the **active segment** in chain order
    - method: `iter_all_events(self, decrypt: bool=False) -> Iterator[AuditEvent]` — Yield events across **all** segments, oldest segment first (G10)
    - method: `_iter_segment(self, segment: Path, decrypt: bool) -> Iterator[AuditEvent]` — Yield the events of one segment file (module helper)
    - method: `verify_all_segments(self) -> list[Any]` — Verify every segment's chain, oldest first (G10)
    - method: `_verify_segment(self, segment: Path) -> Any` — Verify one segment file's chain (module helper)
    - method: `close(self) -> None` — Close the append handle (idempotent)


#### `case_engine.audit.memory_ledger.py` — 139 LOC

**Role:** In-memory audit ledger (tests, demos, CI).  Not durable by design.

**Contents:**

- Exports `__all__`: `InMemoryLedger`
  - **class InMemoryLedger** — Volatile ledger implementing the identical chain contract
    - constructor: `__init__(self) -> None` — Start an empty chain
    - method: `append(self, event_type: str, session_id: str, node: str, payload: dict[str, Any]) -> AuditEvent` — Append one chained event in memory
    - method: `iter_events(self) -> Iterator[AuditEvent]` — Yield stored events in chain order
    - method: `close(self) -> None` — No resources to release


#### `case_engine.audit.sqlite_ledger.py` — 323 LOC

**Role:** SQLite-backed audit ledger (queryable, single-file, ACID appends).

**Contents:**

- Exports `__all__`: `SQLiteLedger`
  - **class SQLiteLedger** — SQLite ledger with the same chain contract as :class:`JSONLLedger`
    - constructor: `__init__(self, path: str | Path=':memory:', payload_cipher: Any | None=None) -> None` — Open the database, ensure the schema, replay the chain head
    - method: `_replay(self) -> None` — Restore ``_seq``/``_last_hash`` from stored rows
    - method: `append(self, event_type: str, session_id: str, node: str, payload: dict[str, Any]) -> AuditEvent` — Append one chained event inside an exclusive lock
    - method: `iter_events(self, decrypt: bool=False) -> Iterator[AuditEvent]` — Yield every event in chain order (payload JSON re-parsed)
    - method: `close(self) -> None` — Close the connection (idempotent)


---

## 8. Pipeline layer — deterministic 8-node control plane

Control flow the LLM cannot influence (Strategy 2). `pipeline/nodes.py` holds the eight pure node functions (classify → route → inject → generate → validate → reflect → synthesize / escalate); `pipeline/state_machine.py` executes them along the `EDGES` table and raises `TopologyViolationError` on any illegal transition; `pipeline/context.py` bundles collaborators and enforces the fail-closed audit contract; `langgraph_adapter.py` re-expresses the same graph for LangGraph users with result-parity guaranteed by tests.


#### `case_engine.pipeline.py` — 41 LOC

**Role:** Pipeline subpackage — the deterministic control plane of the engine.

**Contents:**

- Exports `__all__`: `EngineContext`, `CASEStateMachine`, `EDGES`, `NODES`, `build_case_graph`, `run_case_graph`, `ENTRY_NODE`, `END_NODE`


#### `case_engine.pipeline.context.py` — 222 LOC

**Role:** EngineContext: the dependency bundle threaded through every node.

**Contents:**

- Exports `__all__`: `EngineContext`
  - **class EngineContext** — Shared services + counters for one engine instance
    - method: `audit(self, event_type: AuditEventType, session_id: str, node: str, payload: dict) -> str` — Append one event to the ledger — **fail-closed**
- **def** `_default_sink() -> Any` — Return the default escalation sink (imported lazily at call time)


#### `case_engine.pipeline.langgraph_adapter.py` — 237 LOC

**Role:** Optional LangGraph adapter (Strategy 2 reference implementation).

**Contents:**

- Exports `__all__`: `build_case_graph`, `run_case_graph`
- **def** `_require_langgraph() -> Any` — Import and return the langgraph module or raise a clear error
- **def** `build_case_graph(ctx: EngineContext) -> Any` — Compile the C.A.S.E. pipeline as a LangGraph ``StateGraph``
- **def** `run_case_graph(ctx: EngineContext, state: dict) -> dict` — Compile and run the graph in one call


#### `case_engine.pipeline.nodes.py` — 1504 LOC

**Role:** The 8 pipeline nodes of the C.A.S.E. execution pipeline.

**Contents:**

- Exports `__all__`: `NODES`, `ENTRY_NODE`, `END_NODE`, `node_1_intent_classifier`, `node_2_model_router`, `node_3_schema_injection`, `node_4_llm_generation`, `node_5_ast_validator`, `node_6_reflexion_loop`, `node_7_audit_and_synthesis`, `node_8_human_escalation`
- **def** `_copy(state: dict) -> dict` — Return a shallow copy of *state* (nodes never mutate their input)
- **def** `_truncate(value: Any, limit: int=120) -> str` — Return ``str(value)`` collapsed to one line and truncated to *limit*
- **def** `_payload_hash(payload: dict[str, Any]) -> str` — Return the SHA-256 of a payload's canonical JSON (audit aid)
- **def** `_user_prompt(state: dict, feedback: str | None) -> str` — Compose the user prompt: problem, optional context, optional feedback
- **def** `_record_usage(state: dict, call: Any) -> None` — Accumulate one LLM call's token usage into the session state (G7)
- **def** `_usage_event_payload(state: dict) -> dict[str, int]` — Return the JSON-safe LLM_USAGE payload for the terminal nodes
- **def** `_trace_record(model_def: MentalModelDefinition, payload: dict, attempts: int) -> dict` — Build a serialized TraceRecord for pipeline state
- **def** `node_1_intent_classifier(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Classify the query into a Cynefin domain (spec ``node_1``)
- **def** `node_2_model_router(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Route the classified domain to mental models (spec ``node_2``)
- **def** `node_3_schema_injection(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Register the selected contracts as tool specs (spec ``node_3``)
- **def** `node_4_llm_generation(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Generate one gated tool call per selected model (spec ``node_4``)
- **def** `node_5_ast_validator(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Validate every trace: Pydantic parse + rule battery (spec ``node_5``)
- **def** `node_6_reflexion_loop(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Append feedback, count the retry, route back or escalate (spec ``node_6``)
- **def** `_summarize_trace(model_def: MentalModelDefinition, record: dict) -> str` — Render one trace's ``summary_fields`` for the final response
- **def** `node_7_audit_and_synthesis(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Fire audit hooks, persist traces, synthesize the answer (spec ``node_7``)
- **def** `node_8_human_escalation(state: dict, ctx: EngineContext) -> tuple[str, dict]` — Package partial traces + errors for a human reviewer (spec ``node_8``)


#### `case_engine.pipeline.state_machine.py` — 307 LOC

**Role:** The deterministic state machine (Strategy 2: topology as enforcement).

**Contents:**

- Exports `__all__`: `EDGES`, `CASEStateMachine`
  - **class CASEStateMachine** — Runner enforcing the declared topology over the 8 nodes
    - constructor: `__init__(self, ctx: EngineContext) -> None` — Bind the machine to an engine context
    - method: `run(self, state: dict, max_steps: int=64) -> dict` — Execute the pipeline until ``END`` or the step guard trips
    - method: `async run_async(self, state: dict, max_steps: int=64) -> dict` — Async twin of :meth:`run` — non-blocking event loop (G5, phase 1)


---

*Generated from the AST of the package sources (function signatures and docstring first lines); narrative sections curated by hand. Regenerate with `python scripts/gen_file_reference.py` after API changes.*
