# Contributing to the C.A.S.E. Engine

Thank you for considering a contribution. This document is the fast path
from clone to merged PR; the deep rules live in
[`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) (log levels, exception
hierarchy, fail-closed semantics) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
(the eight-node pipeline and why it is deterministic).

## 1. Development setup

```bash
git clone <your-fork> && cd case-engine
python -m venv .venv && source .venv/bin/activate
pip install -e ".[crypto,server,dev]"     # engine + optional extras + tooling
```

Python **3.11+** is required (3.11/3.12/3.13 are tested in CI). The only
hard runtime dependency is **pydantic ≥ 2.5** — everything else
(providers, LangGraph, crypto, FastAPI) is an optional extra, and the
full test suite runs offline against the mock provider.

## 2. The verification gate (run before every push)

CI (`.github/workflows/ci.yml`) runs the same commands on 3.11/3.12/3.13;
a PR that fails any of these locally will fail there too:

```bash
python -m compileall -q case_engine/ tests/ examples/     # syntax gate
ruff check case_engine/ tests/ examples/                   # lint
mypy case_engine/                                          # static types
python -m pytest tests/ -p no:cacheprovider                # tests
python -m pytest --doctest-modules case_engine/ -p no:cacheprovider
for ex in examples/*.py; do python "$ex"; done             # offline demos
```

Lint/type configuration lives in `pyproject.toml` (`[tool.ruff]`,
`[tool.mypy]`). Fix root causes rather than adding `# type: ignore` /
`# noqa`; every suppression needs a written justification on the same
line, and stale ones are flagged automatically (`RUF100`).

## 2b. Assurance rounds (run per release, not per PR)

Three heavier gates exist for releases; CI runs only the smoke variants:

```bash
python bench/run_bench.py --profile standard --md bench_results.md   # G25 numbers
python tools/mutation_run.py                                          # G28 baseline (docs/MUTATION_BASELINE.md)
python -m pytest tests/unit/test_property_gate.py                     # G22 (needs `pip install hypothesis`)
python -m tests.adversarial.report                                    # G23 corpus replay table
```

A mutation score below the last documented baseline (0.98) is a release
blocker: either strengthen the rule tests or justify the survivor in
`JUSTIFIED_EQUIVALENTS` inside `tools/mutation_run.py`. Bench numbers are
machine-indicative — compare runs on the same host only.

## 3. Ground rules (enforced in review)

1. **Fail-closed, never fail-silent.** A validation/audit/infrastructure
   failure stops the session or routes it to escalation — it is never
   swallowed. See `docs/CONVENTIONS.md` §"Exception hierarchy": raise
   specific `CASEError` subclasses with structured `context`, log at
   ERROR *before* raising, and chain with `raise ... from exc`.
2. **Log-unsafe data stays out of logs.** No query text, prompt
   content, PII or secrets in log records — use counts, ids, truncated
   previews (`_short`) and kwarg *names*. `EngineContext.audit()`'s
   sanitization hooks apply the same rule to ledger payloads.
3. **Deterministic control plane.** The LLM may only fill typed tool
   schemas; routing, topology, step bounds and validation are pure
   code. If your change touches the pipeline, keep the `EDGES` table
   authoritative and the state machine's step bound intact.
4. **Every behavior change ships with tests.** New features need
   hermetic unit tests (in-memory / tmp-path backends, tiny timeouts,
   no network); bug fixes need a regression test that fails without
   the fix. Docstrings are Google style and doctests must stay green.
5. **Zero new hard dependencies.** Provider/SDK imports stay lazy and
   optional extras stay optional; stdlib-first is the default.
6. **Stable identifiers are never renamed.** `MM###`/`VR###` ids and
   the `AuditEvent` schema are contract; additive evolution only (see
   `AUDIT_EVENT_SCHEMA_VERSION` in `case_engine/audit/base.py`).

## 4. Pull requests

- One logical change per PR; update `CHANGELOG.md` (Keep-a-Changelog
  sections) and the relevant `docs/*.md` page in the same PR.
- If you add or move a module, regenerate the API reference:
  `python scripts/build_file_reference.py` (the repo owner runs it from
  the packaging scripts; keep `docs/FILE_REFERENCE.md` in sync).
- CI must be green; the maintainer review focuses on the ground rules
  above, not formatting (that is ruff's job).

## 5. Reporting issues

Include: engine version (`case_engine.__version__`), Python version, the
minimal reproducer (a failing test is ideal), and the audit-ledger
verification output (`case-engine verify`) when the report involves a
session outcome. **Never** attach real query content, PII or provider
keys — see [`SECURITY.md`](SECURITY.md) for security-sensitive reports.
