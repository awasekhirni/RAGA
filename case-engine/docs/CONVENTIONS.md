# Engineering Conventions

The C.A.S.E. engine holds itself to the same constitution it enforces on LLMs:
**deterministic behavior, explicit contracts, full observability.** This
document defines the standards every module follows — logging, error handling,
documentation and the fail-closed semantics that make the engine auditable.
They are not aspirations: a machine-verified snapshot of compliance lives in
[`FILE_REFERENCE.md`](FILE_REFERENCE.md) (100% docstring coverage across 168
functions, 325 structured-logging call sites).

---

## 1. Logging

### 1.1 Namespace & acquisition

Every module acquires exactly one logger at import time, namespaced under the
engine root so host applications can route engine output independently:

```python
from case_engine.logging_setup import get_logger

log = get_logger(__name__)          # -> "case_engine.<submodule>"
```

`configure_logging()` is the *only* function that installs handlers; it is
idempotent, replaces prior engine handlers, and never touches handlers owned
by the host application. Library etiquette: the engine configures logging only
when the `case_engine` logger has no handlers yet.

### 1.2 Level contract

| Level | Meaning | Examples |
|---|---|---|
| `DEBUG` | Function entry with key parameters, per-item loops, decision branches, state reads, payload *shapes* (never full content) | `routing task`, `rule evaluated`, `prompt built (chars=2142)` |
| `INFO` | Milestones only | `CASEEngine initialised`, `node completed`, `audit event appended`, `audit chain verified OK`, `session complete` |
| `WARNING` | Rejections, fallbacks, degradation, budget exhaustion | gate rejected a hallucinated tool, `Validation rule rejected trace`, judge degraded to heuristic-only, reflexion budget exhausted, chain re-anchored at genesis |
| `ERROR` | Infrastructure failure immediately before raising (usually with `exc_info=True`) | ledger append failed, provider SDK error, topology violation attempted |
| `CRITICAL` | Reserved: failure of the audit machinery itself | ledger append failure inside the audit path |

CLI note: `case-engine` binds engine logs to **stderr** and keeps stdout for
machine-readable results (`--json`), so log noise can never corrupt parsed
output.

### 1.3 Structured extras

Messages are short and stable; the specifics go into `extra={...}` with
snake_case keys so both formatters (console `k=v`, JSON lines) can ship them:

```python
log.warning(
    "Validation rule rejected trace",
    extra={"rule_id": "VR004", "session_id": sid, "node": "node_5",
           "reason": _short(message)},
)
```

Standard keys used across the codebase: `session_id`, `node`, `model_id`,
`rule_id`, `event_type`, `seq`, `path`, `provider`, `latency_ms`, `reason`.

### 1.4 Log-safety rules (hard requirements)

1. **Never log secrets.** API keys and provider credentials never appear in
   any record; `create_llm_client` logs kwarg *names* only.
2. **Never log full prompts/completions above DEBUG — and truncate at DEBUG
   too.** Helpers `_short()` / `_truncate()` enforce a bound (≈160 chars) at a
   single point per module.
3. **Never use reserved `LogRecord` attribute names as extras** (`message`,
   `name`, `args`, `asctime`, `exc_info`, …) — Python raises
   `KeyError: Attempt to overwrite ...` when the level is enabled.
4. Hashes and identifiers are safe to log (prefix only); payloads are
   summarized by field *count*, never content.

---

## 2. Error handling

### 2.1 The exception hierarchy

Everything the engine raises is a `CASEError` subclass
(`case_engine/exceptions.py`), each carrying a stable `default_message`:

| Exception | Raised by | Absorb path |
|---|---|---|
| `ConfigurationError` | config load/factories | never |
| `RegistryError` | registry lookups/consistency | never |
| `RoutingError` | ModelRouter (forced/unknown domain) | node_2 → escalation |
| `LLMProviderUnavailableError` | provider adapters | node_4 → reflexion |
| `LLMResponseError` | malformed provider replies | node_4 → reflexion |
| `ToolCallRejectedError` | SchemaGate (Strategy 1) | node_4 → reflexion |
| `SchemaViolationError` | validation battery | node_5 → reflexion |
| `LedgerError` | audit backends | **fail-closed: never absorbed** |
| `PipelineError` | state machine infrastructure | never |
| `TopologyViolationError` | illegal EDGES transition | never (programmer error) |

### 2.2 The two failure classes

**Reasoning failures** (bad generation, rejected tool call, failed rules,
exhausted reflexion) are *data, not exceptions*: they flow to node_6
(reflection), fall back to the *failed* node (not blindly node_4), and on
budget exhaustion escalate to node_8 with partial traces attached.
`CASEEngine.run` **never raises** for these — it returns an escalated result.

**Infrastructure failures** (ledger unavailable, provider unreachable past
retry, bad config, illegal transition) raise after being logged at `ERROR`
with context. The audit contract is **fail-closed**: if the ledger cannot
record a step, the session halts — the engine never advances unaudited.

### 2.3 Mechanics (enforced in review & tests)

- Catch **specific** exceptions only; bare `except:` is forbidden.
- Chain causes explicitly: `raise LedgerError(...) from exc` — original
  tracebacks survive for operators.
- Log before raising (`ERROR` + `exc_info=True` where the traceback helps);
  the raising function is the one that logs.
- `finally` releases ledger/HTTP resources; ledgers are additionally
  idempotent-safe on `close()`.
- Validation infrastructure errors are **never** converted into a "pass"
  verdict (fail-closed battery); the *optional* LLM judge is the single,
  explicitly documented fail-open component — it can veto, never rescue.

---

## 3. Documentation

### 3.1 Docstrings — Google style, everywhere

Every module, class, and function (168/168) carries a Google-style docstring:

- **Module docstring**: purpose & architectural role, a *Contents* list,
  the *invariants* the module upholds, and cross-references to `docs/*.md`
  and sibling modules.
- **Functions/methods**: one-line summary; extended prose (what/why/
  invariants/side effects); `Args:` with constraints; `Returns:` with
  semantics; `Raises:` for every exception *including when it is logged*;
  `Example:` as a doctest where meaningful (62 executable doctests run in
  CI-style validation; environment-coupled examples are marked `+SKIP`
  deliberately, with the reason stated in prose).
- **Pydantic models**: every field documented via `Field(description=...)`
  — these descriptions intentionally flow into the generated LLM tool JSON
  schemas, improving provider-side tool selection.
- **Validation rules**: each `_vrNNN` documents its checks, expected inputs,
  pass/fail conditions, the contract it protects, and a concrete fail example.

### 3.2 Executable documentation

```bash
pytest --doctest-modules case_engine      # 69 passed, 12 skipped
pytest tests/ -q                          # 189 tests, 90% coverage, offline
```

### 3.3 Keeping the reference honest

`docs/FILE_REFERENCE.md` is generated from the package AST (signatures +
docstring first lines) and curated per layer. After any API change,
regenerate it — a stale reference is treated as a bug.

---

## 4. Quick reference card

```python
# The pattern every module follows
from case_engine.logging_setup import get_logger

log = get_logger(__name__)

def do_work(task_id: str, payload: dict[str, Any]) -> int:
    """One-line summary.

    Why this exists / invariants it upholds.

    Args:
        task_id: Identifier of the task (non-empty).
        payload: Bounded payload; only field *count* is ever logged.

    Returns:
        Number of processed items.

    Raises:
        LedgerError: If the audit append fails (fail-closed; logged at
            ERROR with ``exc_info`` before raising).

    Example:
        >>> do_work("t-1", {"k": 1})          # doctest: +SKIP
        1
    """
    log.debug("do_work entry", extra={"task_id": task_id,
                                      "payload_fields": len(payload)})
    try:
        result = _process(payload)
    except OSError as exc:
        log.error("processing failed", extra={"task_id": task_id}, exc_info=True)
        raise LedgerError(f"processing failed for {task_id}") from exc
    log.info("do_work completed", extra={"task_id": task_id, "items": result})
    return result
```
