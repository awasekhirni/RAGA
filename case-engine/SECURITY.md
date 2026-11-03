# Security Policy

## Supported versions

| Version | Line | Security fixes |
|---|---|---|
| 2.3.x | current | ✅ yes |
| 2.2.x | previous | ✅ until 2.4.0 |
| < 2.2 | legacy | ❌ upgrade first (P0/P1 data-protection enforcement landed in 2.1.0/2.2.0) |

## Reporting a vulnerability

**Do not open a public issue for security reports.**

1. Email the maintainer listed in `pyproject.toml` with the subject
   `[case-engine security] <short summary>`.
2. Include: affected version, a minimal reproducer, impact assessment,
   and (if relevant) the *structure* of the audit evidence — never real
   payloads, query content, PII or keys.
3. You will receive an acknowledgement within 5 business days and a
   fix timeline within 15; coordinated disclosure is the default and
   reporters are credited (opt-in).

## Threat model — what the engine defends against

The C.A.S.E. engine assumes the LLM is **untrusted**: it may hallucinate
tools, emit malformed payloads, leak prompt-injected instructions, or
refuse. The defenses are code, not prompts:

| Layer | Guarantee | Where |
|---|---|---|
| Schema gate | Only typed tool schemas pass; `extra="forbid"`, no free-text detours | `enforcement/gate.py` |
| Validation rules | 16 deterministic rules gate reasoning quality; fail-closed | `validation/rules.py` |
| Reflexion → escalation | Bounded retries, then a typed human handoff (never silent degradation) | `pipeline/nodes.py`, `escalation.py` |
| Hash-chained audit | Every step is append-only evidence; `verify` detects tampering | `audit/` |
| PII/TEE gate (G1) | Query/context scanned before any LLM call; policy `escalate` or `allow_with_flags` | `privacy.py` |
| Redaction + envelope encryption (G4) | Sensitive ledger payloads encrypted at rest, redacted in logs | `privacy.py`, `audit/crypto.py` |
| Prompt-injection surface | Untrusted user data is fenced in prompts (`<untrusted_user_data>`); see `docs/CONTEXT_ENGINEERING.md` CE-02 | `pipeline/nodes.py` |
| External anchoring (G10) | Head-hash anchors let third parties detect full-chain rewrites | `audit/anchor.py` |

## Known caveats (read before deploying)

- **Ledger write access = rewrite capability.** A local attacker with
  filesystem write access can rewrite the whole JSONL chain *unless*
  external anchoring (`JSONLLedger(anchor_sink=...)`, G10) is configured
  to a store they cannot rewrite. Use SQLite + OS permissions + an
  external `AnchorSink` for tamper-*evident* deployment.
- **Multi-writer ledgers.** The JSONL backend is cross-process safe via
  advisory `flock` (G9) but is *single-host*; use the SQLite backend for
  multi-writer deployments.
- **Judge fail-open.** The optional LLM judge can only veto, and its own
  failure degrades to heuristic-only validation (G3/G13 budget guard);
  the deterministic rules always run first.
- **PII policy is per-deployment.** The default `pii_policy` and
  `tee_attested` flags must be set explicitly by operators (see
  `EngineConfig`); the engine never silently relaxes them.
- **Secrets.** API keys are passed via `llm_kwargs` and are never
  logged (kwarg names only); supply keys through your own secret
  manager, not the audit ledger or result store.

## Scope

In scope: the `case_engine` package, its CLI, the optional FastAPI
serving layer (`case_engine[server]`), and the ledger/result-store
file formats. Out of scope: the LLM providers themselves, LangGraph,
and the host OS/runtime.
