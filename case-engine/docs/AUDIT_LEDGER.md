# The Audit Ledger

Every C.A.S.E. session writes an append-only, **hash-chained** event trail.
The ledger is the evidence layer: it proves *what the engine did, in what
order, on what evidence* — and it proves tampering after the fact.

---

## 1. Chain format

Each entry is an `AuditEvent`:

```json
{
  "seq": 7,
  "timestamp": "2026-01-15T09:30:12.123456+00:00",
  "event_type": "TRACE_VALIDATED",
  "session_id": "8f0c7e52-...",
  "node": "node_5_ast_validator",
  "payload": {"model": "MM005", "rules": ["VR005", "VR016"]},
  "prev_hash": "9d3f...a1",
  "hash": "c7b2...4e"
}
```

Chain equation (implemented in `audit/base.py: compute_event_hash`):

```
hash_n = SHA-256( canonical_json({
    prev_hash: hash_{n-1}, seq: n, timestamp, event_type,
    session_id, node, payload }) )
```

- `hash_0.prev_hash = "0" * 64` (genesis anchor).
- `canonical_json` sorts keys and uses compact separators, so the same
  logical event hashes identically across processes and backends.
- Editing, deleting or reordering **any** historical entry breaks every
  subsequent hash → `verify_chain()` fails with the first bad `seq`.

## 2. Backends

| Backend | Class | Durability | Query story | Use when |
|---|---|---|---|---|
| `jsonl` | `JSONLLedger` | fsync per append | `grep`/`jq`, git-diffable | Default; human-readable ops trails |
| `sqlite` | `SQLiteLedger` | ACID commits | SQL (`GROUP BY event_type`, joins) | Server deployments, dashboards |
| `memory` | `InMemoryLedger` | none | in-process | Tests, CI, ephemeral demos |

All three implement the identical `AuditLedger` contract
(`append / iter_events / verify_chain / close`) and are selected by
`EngineConfig.ledger_backend` + `ledger_path` (defaults:
`./case_audit.jsonl`, `./case_audit.sqlite3`).

`JSONLLedger` replays an existing file on open (chain continues across
process restarts); `SQLiteLedger` does the same from its table.

## 3. Verification

```python
engine.verify_audit_chain()          # -> ChainVerification
```

```bash
case-engine verify --ledger-path case_audit.jsonl
# OK — 16 events verified, hash chain intact
```

On tampering:

```
TAMPERED — Hash mismatch at seq=2: payload was modified (first bad seq: 2)
```

`verify_chain()` checks, in order: sequence continuity (1..n), prev-hash
linkage, per-entry hash recomputation. It reports the **first** failure and
stops — everything after a break is untrusted by definition.

`tests/unit/test_ledgers.py` proves tamper detection for both persistent
backends (JSONL line rewrite; SQL `UPDATE`), corrupt-file rejection, and
cross-restart chain continuity.

## 4. Event vocabulary

`SESSION_START`, `DOMAIN_PRESET`, `DOMAIN_CLASSIFIED`, `MODELS_SELECTED`,
`SCHEMAS_INJECTED`, `TRACE_GENERATED`, `TRACE_REJECTED`,
`TRACE_VALIDATED`, `VALIDATION_FAILED`, `REFLEXION_RETRY`,
`TRACE_LEDGER_WRITE`, `HOOK_FIRED`, `ESCALATED`, `SESSION_COMPLETED`,
`SESSION_FAILED` — plus the v2.1.0 additions `TEE_DECISION` /
`FALLBACK_APPLIED` and the v2.2.0 addition `LLM_USAGE` (per-session token
accounting: `{calls, prompt_tokens, completion_tokens, total_tokens}`;
counts only, emitted by the terminal nodes on success *and* escalation)
(see `schemas/pipeline.py: AuditEventType` — a stable contract for
SIEM/analytics).

Useful sessions queries (SQLite):

```sql
-- lifecycle of one session
SELECT seq, event_type, node FROM audit_events
WHERE session_id = ? ORDER BY seq;

-- reflexion hotspots across all sessions
SELECT model, COUNT(*) FROM audit_events
WHERE event_type = 'VALIDATION_FAILED'
GROUP BY 1 ORDER BY 2 DESC;

-- per-session cost (v2.2.0 LLM_USAGE)
SELECT session_id, payload FROM audit_events
WHERE event_type = 'LLM_USAGE';
```

## 4b. Concurrency, rotation & external anchoring (v2.2.0, G9/G10)

**Cross-process appends (G9).** `JSONLLedger(cross_process_safe=True)`
(default) takes an advisory `fcntl.flock` on the ledger file for every
append and re-reads the file's last line *under the lock*, adopting an
externally advanced chain tail instead of forking the hash sequence.
Platforms without `fcntl` (Windows) degrade to single-writer with a
one-time DEBUG note — use the SQLite backend for multi-writer
deployments. `cross_process_safe=False` restores the cheapest legacy
path. The engine itself remains single-session-per-instance: run one
session at a time per `CASEEngine`, use `run_async` (G5) for I/O overlap
within a session, and one ledger writer per file across processes.

**Size-based rotation (G10).** `JSONLLedger(max_bytes=N)` rotates the
active file to `<stem>.segN<suffix>` before an overflowing append and
re-anchors the chain at genesis — each segment is an unbroken chain of
its own, and rotation numbering survives restarts. `verify_chain()`
verifies the active segment; `verify_all_segments()` walks every segment
oldest→newest; `iter_all_events()` iterates full history. A single event
larger than `max_bytes` gets its own segment rather than an error.

**External anchoring (G10).** The chain is self-consistent; anchoring is
what makes it *externally provable*. Configure an
`AnchorSink` (`case_engine/audit/anchor.py`) — `FileAnchorSink` ships,
TSA/Git/S3 sinks are one method each — and either call
`ledger.anchor(note=...)` periodically (cron / deployment hook) or let
rotation auto-anchor the closed segment. The anchor record commits
`{head_hash, head_seq, source_path, schema_version, anchored_at}` to the
sink; a verifier compares it against the file head to detect whole-chain
rewrites (the `_replay`-trusts-first-record attack in the original gap).

**Schema version (G10).** `AuditEvent.schema_version` (default 1) rides
outside the frozen hash recipe — old ledgers replay byte-compatibly —
and each `SESSION_START` payload carries `audit_event_schema_version`
*inside* the hashed material, making the version itself tamper-evident.

## 5. Payload-hashing and PII (v2.1.0: enforced, not just declared)

`TRACE_GENERATED` / `TRACE_LEDGER_WRITE` events store the payload's
SHA-256 (`payload_hash`), not the payload, when operators prefer PII to
stay out of the trail; the escalation package (node_8) intentionally
preserves full partial traces because a human reviewer needs them.
Since **v2.1.0** the data-protection promises are enforced in code
(G1–G4, see `docs/GAP_ANALYSIS.md`):

### 5.1 TEE gate (G1)
node_1 scans every query (and context) with a pluggable `PIIDetector`
(offline regex default). PII without TEE attestation follows
`guardrails.pii_policy`: `escalate` (fail-closed refusal before any LLM
call) or `allow_with_flags`. Every sighting is audited as a
**`TEE_DECISION`** event carrying category names only — never raw values.

### 5.2 Payload redaction (G4a)
Set `redact_pii_in_audit=True` and a `PIIRedactor` tokenizes PII out of
every audit payload **before** hashing (deterministic session-scoped
tokens like `[EMAIL_1]`; the mapping table lives in memory only). The
chain therefore commits to exactly the redacted bytes on disk, and
evidence stays joinable per session.

### 5.3 Encryption at rest (G4b)
Set `ledger_encryption=True` (`pip install 'case-engine[crypto]'`, key in
`CASE_ENGINE_LEDGER_KEY`) and payloads become AES-256-GCM envelopes
(`{"__enc__": "AESGCM", "nonce": …, "data": …}`) **before** hashing —
encrypt-then-chain. Consequences:

- `verify_chain()` / `case-engine verify` need **no key**: the chain
  hashes the ciphertext exactly as persisted.
- `iter_events(decrypt=True)` returns plaintext; a wrong key or tampered
  ciphertext raises `LedgerError` (fail-closed) — and a tampered
  ciphertext *also* breaks the hash chain, so mutation is doubly visible.
- Event metadata (seq, type, session, node, timestamp) remains readable
  without the key by design.

Both layers compose: redaction shrinks what is sensitive, encryption
protects whatever remains. Demonstration: `examples/pii_protection.py`.

## 6. Fail-closed auditing

`EngineContext.audit()` treats a ledger failure as fatal: the session
aborts rather than continue unaudited (zero-trust posture). Test:
`tests/unit/test_ledgers.py::test_ledger_failure_fails_engine_closed`.

## 7. DPO dataset extraction (spec Strategy 5)

The chain is exactly the labelled corpus a DPO pipeline needs:

- preferred examples: `TRACE_VALIDATED` events (with payload hashes and
  model ids),
- rejected examples: `TRACE_REJECTED` / `VALIDATION_FAILED` events with the
  failing rule ids as the rejection rationale.

Export with a small script over `iter_events()` — the canonical JSON makes
the export deterministic.
