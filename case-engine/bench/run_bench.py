"""C.A.S.E. performance benchmark harness (G25).

Publishes the latency numbers the project previously refused to claim:
per-node p50/p95/p99, full-pipeline latency/throughput with the offline
mock provider, enforcement-gate overhead, hash-chain primitive cost and
per-backend ledger append cost (including the fsync'd JSONL path).

Usage
-----
    python bench/run_bench.py                       # standard profile
    python bench/run_bench.py --profile smoke       # quick CI-safe run
    python bench/run_bench.py --json out.json --md out.md

Everything runs offline (mock provider, temporary ledgers, no
network); the harness never mutates repo files unless an explicit
``--json``/``--md`` output path is given.  Numbers are indicative for
the machine that produced them — the report embeds environment details
so capacity claims stay attributable (see ``bench/README.md``).

Exit codes: ``0`` clean run, ``2`` harness failure (any unexpected
exception; logged with traceback before exit).

Cross-references: ``docs/GAP_ANALYSIS.md`` §G25,
``tests/integration/test_bench.py`` (smoke regression).
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from case_engine.audit import InMemoryLedger, JSONLLedger, SQLiteLedger
from case_engine.audit.base import canonical_json, compute_event_hash
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.enforcement.gate import SchemaGate
from case_engine.engine import CASEEngine
from case_engine.llm import MockLLMClient
from case_engine.logging_setup import configure_logging, get_logger
from case_engine.observability import InMemoryMetrics
from case_engine.registry import CognitiveRegistry
from case_engine.version import __version__

__all__ = ["run_bench", "main"]

log = get_logger(__name__)

#: Standard benchmark query (mirrors the demo suite).
_QUERY = "Why did our checkout error rate spike last Tuesday?"

#: Profile presets: (pipeline_runs, gate_iterations, ledger_ops).
_PROFILES = {
    "smoke": {"pipeline_runs": 10, "gate_iterations": 300, "ledger_ops": (100, 50, 100, 50)},
    "standard": {"pipeline_runs": 50, "gate_iterations": 2000, "ledger_ops": (1000, 200, 1000, 300)},
}

#: Node latency metric emitted by the state machine (G11).
_NODE_METRIC = "case_engine_node_latency_ms"


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of *values* (ms preserved as given).

    Args:
        values: Non-empty sample.
        pct: Percentile in ``[0, 100]``.

    Returns:
        The nearest-rank percentile value; ``0.0`` for an empty sample.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered) - 1, rank - 1)]


def _stats(values: list[float]) -> dict[str, float]:
    """Return ``p50/p95/p99/mean/min/max`` (rounded) for a sample."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "p50": round(_percentile(values, 50), 3),
        "p95": round(_percentile(values, 95), 3),
        "p99": round(_percentile(values, 99), 3),
        "mean": round(statistics.fmean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _timed(fn: Callable[[], Any], iterations: int) -> list[float]:
    """Run *fn* *iterations* times, returning per-call milliseconds."""
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def _bench_pipeline(runs: int, registry: CognitiveRegistry) -> tuple[dict[str, Any], dict[str, list[float]]]:
    """Measure full-pipeline latency and collect per-node samples.

    Args:
        runs: Number of complete ``engine.run`` sessions.
        registry: Pre-loaded registry (shared, read-only).

    Returns:
        ``(pipeline_stats, node_samples)`` where *node_samples* maps
        node id -> list of latency samples in ms.
    """
    metrics = InMemoryMetrics()
    engine = CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
            ledger_backend="memory",
        ),
        llm_client=MockLLMClient.from_registry_examples(registry),
        registry=registry,
        metrics=metrics,
    )
    started = time.perf_counter()
    durations = _timed(lambda: engine.run(_QUERY), runs)
    wall = time.perf_counter() - started

    node_samples: dict[str, list[float]] = {}
    for key, values in metrics.snapshot()["observations"].items():
        if key == _NODE_METRIC:
            node_samples["(all nodes)"] = values
        elif key.startswith(f"{_NODE_METRIC}|"):
            node = dict(pair.split("=", 1) for pair in key.split("|", 1)[1].split(",")).get("node", "?")
            node_samples[node] = values

    pipeline = _stats(durations)
    pipeline["throughput_sessions_per_s"] = round(runs / wall, 2) if wall > 0 else 0.0
    return pipeline, node_samples


def _bench_gate(registry: CognitiveRegistry, iterations: int) -> dict[str, Any]:
    """Measure SchemaGate overhead for approve and reject paths."""
    gate = SchemaGate(registry)
    gold = dict(next(iter(
        m.examples[0] for m in registry.all_models()
        if m.tool_name == "RootCauseAnalysisTrace" and m.examples
    )))
    rejected_payload = {**gold, "extra_smuggled_field": {"directive": True}}

    approved = _timed(lambda: gate.check("RootCauseAnalysisTrace", dict(gold)), iterations)
    rejected = _timed(
        lambda: gate.check("RootCauseAnalysisTrace", dict(rejected_payload)), iterations
    )
    return {"approved": _stats(approved), "rejected": _stats(rejected)}


def _bench_hash_primitives(iterations: int) -> dict[str, Any]:
    """Measure canonical_json and compute_event_hash cost."""
    payload = {"problem_statement": _QUERY, "why_1": "a", "why_2": "b",
               "why_3": "c", "why_4": "d", "why_5": "e", "confidence": 0.9}
    dump_samples = _timed(lambda: canonical_json(payload), iterations)
    hash_samples = _timed(
        lambda: compute_event_hash("0" * 64, 1, "2026-01-01T00:00:00+00:00",
                                   "TRACE_GENERATED", "bench", "bench", payload),
        iterations,
    )
    return {"canonical_json_us": _stats([s * 1000.0 for s in dump_samples]),
            "compute_event_hash_us": _stats([s * 1000.0 for s in hash_samples])}


def _bench_ledgers(ops: tuple[int, int, int, int]) -> dict[str, Any]:
    """Measure per-append cost across the ledger backends.

    Args:
        ops: ``(memory_ops, jsonl_nofsync_ops, jsonl_fsync_ops, sqlite_ops)``.

    Returns:
        Per-backend append stats plus ``verify_chain`` cost on the
        memory backend.
    """
    memory_ops, jsonl_fast_ops, jsonl_fsync_ops, sqlite_ops = ops
    payload = {"event": "bench", "seq_note": "payload-contents-never-logged"}
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="case-bench-") as tmp:
        memory = InMemoryLedger()
        results["memory"] = _stats(_timed(
            lambda: memory.append("TRACE_GENERATED", "bench", "bench", payload), memory_ops,
        ))

        fast_path = Path(tmp) / "fast.jsonl"
        fast = JSONLLedger(fast_path, fsync=False)
        results["jsonl_nofsync"] = _stats(_timed(
            lambda: fast.append("TRACE_GENERATED", "bench", "bench", payload), jsonl_fast_ops,
        ))
        fast.close()

        durable_path = Path(tmp) / "durable.jsonl"
        durable = JSONLLedger(durable_path, fsync=True)
        results["jsonl_fsync"] = _stats(_timed(
            lambda: durable.append("TRACE_GENERATED", "bench", "bench", payload), jsonl_fsync_ops,
        ))
        durable.close()

        sqlite = SQLiteLedger(Path(tmp) / "bench.db")
        results["sqlite"] = _stats(_timed(
            lambda: sqlite.append("TRACE_GENERATED", "bench", "bench", payload), sqlite_ops,
        ))
        verify_samples = _timed(lambda: memory.verify_chain(), max(10, memory_ops // 10))
        results["memory_verify_chain"] = _stats(verify_samples)
    return results


def run_bench(profile: str = "standard") -> dict[str, Any]:
    """Execute the full benchmark and return the report document.

    Args:
        profile: ``"smoke"`` or ``"standard"`` preset.

    Returns:
        A JSON-safe report dict with ``meta`` and one section per
        measured surface (``pipeline``, ``per_node``, ``gate``,
        ``hash_primitives``, ``ledgers``).

    Raises:
        ValueError: On an unknown profile name.
    """
    if profile not in _PROFILES:
        log.error("unknown bench profile", extra={"profile": profile})
        raise ValueError(f"Unknown profile: {profile!r} (expected {sorted(_PROFILES)})")
    preset = _PROFILES[profile]
    registry = CognitiveRegistry.load_default()

    log.info("bench starting", extra={"profile": profile})
    pipeline, node_samples = _bench_pipeline(preset["pipeline_runs"], registry)
    report: dict[str, Any] = {
        "meta": {
            "profile": profile,
            "case_engine_version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "timestamp": datetime.now(UTC).isoformat(),
            "note": "mock provider; numbers are machine-indicative, not SLOs",
        },
        "pipeline": pipeline,
        "per_node": {node: _stats(samples) for node, samples in sorted(node_samples.items())},
        "gate": _bench_gate(registry, preset["gate_iterations"]),
        "hash_primitives": _bench_hash_primitives(preset["gate_iterations"]),
        "ledgers": _bench_ledgers(preset["ledger_ops"]),
    }
    log.info("bench finished", extra={"profile": profile})
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    """Render the report dict as a compact markdown document.

    Args:
        report: Report document as produced by :func:`run_bench`.

    Returns:
        Markdown string with one table per section.
    """
    lines: list[str] = [
        "# C.A.S.E. engine — benchmark report (G25)",
        "",
        f"- profile: `{report['meta']['profile']}` · engine `{report['meta']['case_engine_version']}`"
        f" · python {report['meta']['python']}",
        f"- platform: `{report['meta']['platform']}`",
        f"- timestamp: {report['meta']['timestamp']}",
        f"- note: {report['meta']['note']}",
        "",
        "## Full pipeline (offline mock provider)",
        "",
        "| metric | ms (except where noted) |",
        "| --- | --- |",
    ]
    for key, value in report["pipeline"].items():
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Per-node latency", "", "| node | p50 | p95 | p99 | mean |", "| --- | --- | --- | --- | --- |"]
    for node, stats in report["per_node"].items():
        lines.append(f"| {node} | {stats['p50']} | {stats['p95']} | {stats['p99']} | {stats['mean']} |")
    lines += ["", "## Enforcement gate overhead", "", "| path | p50 | p95 | p99 | mean |", "| --- | --- | --- | --- | --- |"]
    for path, stats in report["gate"].items():
        lines.append(f"| {path} | {stats['p50']} | {stats['p95']} | {stats['p99']} | {stats['mean']} |")
    lines += ["", "## Hash-chain primitives", "", "| primitive | p50 | p99 | mean (µs) |", "| --- | --- | --- | --- |"]
    for name, stats in report["hash_primitives"].items():
        lines.append(f"| {name} | {stats['p50']} | {stats['p99']} | {stats['mean']} |")
    lines += ["", "## Ledger append cost", "", "| backend | ops p50 | p95 | p99 | mean |", "| --- | --- | --- | --- | --- |"]
    for backend, stats in report["ledgers"].items():
        lines.append(f"| {backend} | {stats['p50']} | {stats['p95']} | {stats['p99']} | {stats['mean']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``2`` on harness failure.
    """
    parser = argparse.ArgumentParser(
        prog="python bench/run_bench.py",
        description="C.A.S.E. engine performance benchmark (G25).",
    )
    parser.add_argument("--profile", choices=sorted(_PROFILES), default="standard",
                        help="Preset: 'standard' (default) or 'smoke' for CI.")
    parser.add_argument("--json", type=Path, default=None, help="Write the raw report JSON here.")
    parser.add_argument("--md", type=Path, default=None, help="Write a markdown report here.")
    args = parser.parse_args(argv)

    configure_logging(level="ERROR")  # bench output is the report, not engine logs
    try:
        report = run_bench(args.profile)
    except Exception as exc:
        log.error("bench failed", extra={"error": str(exc)}, exc_info=True)
        print(f"BENCH ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"json report -> {args.json}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(_render_markdown(report), encoding="utf-8")
        print(f"markdown report -> {args.md}")

    print(_render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
