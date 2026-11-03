# C.A.S.E. engine — performance benchmarks (G25)

This directory holds the benchmark harness that publishes the latency
numbers the project previously refused to claim.  Numbers are
**machine-indicative, not SLOs**: every report embeds platform metadata
so a capacity claim is always attributable to the host that produced
it.  Compare runs on the same host only.

## Usage

```bash
python bench/run_bench.py --profile standard --md bench_results.md --json bench_results.json
python bench/run_bench.py --profile smoke      # CI-safe quick run (~10 s)
```

Everything runs offline (mock provider, temporary ledgers, no network,
no repo files touched unless `--json`/`--md` are given).

## What is measured

| Section | What it answers |
| --- | --- |
| Full pipeline | End-to-end `engine.run` latency (p50/p95/p99/mean) and sessions/s with the offline mock provider. |
| Per-node latency | p50/p95/p99 per pipeline node, captured through the G11 metrics hooks (`case_engine_node_latency_ms`). |
| Enforcement gate | `SchemaGate.check` overhead for the approve and reject paths — the per-call cost of Strategy 1. |
| Hash-chain primitives | `canonical_json` and `compute_event_hash` microbenchmarks (µs). |
| Ledger append | Per-append cost for `memory`, JSONL buffered, JSONL **fsync'd** and SQLite backends, plus `verify_chain` cost. |

## Baseline (standard profile, 2026-11-03)

Recorded on the v2.4.0 release host (`Linux x86_64`, Python 3.12.16);
regenerate on your own hardware before quoting:

- **Full pipeline**: p50 2.5 ms · p95 3.2 ms · p99 5.7 ms · ~380 sessions/s
- **Per node** (p50 ms): `node_1` classifier 0.51 · `node_2` router 0.06 ·
  `node_3` schema injection 1.00 · `node_4` generation 0.34 ·
  `node_5` validation 0.25 · `node_7` synthesis 0.18
- **Gate overhead**: ~0.010 ms per check (approve and reject paths alike —
  rejection costs nothing extra)
- **Hash primitives**: `canonical_json` ~3.9 µs · `compute_event_hash` ~7.0 µs
- **Ledger append** (p50 ms): memory 0.011 · JSONL buffered 0.063 ·
  JSONL fsync'd 0.082 · SQLite 0.067; `verify_chain` (memory, 1k events) ~6.3 ms

Reading: the deterministic scaffolding (nodes 1–5, 7) costs ~2.5 ms per
session; a fsync'd audit event costs ~0.08 ms (≈12 500 durable appends/s
on this host); the zero-trust gate adds ~1 % of one node's latency.

Cross-references: `docs/GAP_ANALYSIS.md` §G25,
`tests/integration/test_bench.py` (smoke regression).
