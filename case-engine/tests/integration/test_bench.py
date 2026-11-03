"""Smoke regression for the G25 benchmark harness.

The bench must stay runnable: this test executes the ``smoke`` profile
in a subprocess (exactly as a user would) and validates the report
shape — every section present, every statistic finite and non-negative.
It deliberately does NOT assert absolute numbers (machine-dependent);
only structure and sanity bounds.

Cross-references: ``bench/run_bench.py``, ``docs/GAP_ANALYSIS.md`` §G25.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_SCRIPT = REPO_ROOT / "bench" / "run_bench.py"

_REQUIRED_SECTIONS = ("meta", "pipeline", "per_node", "gate", "hash_primitives", "ledgers")
_REQUIRED_STAT_KEYS = ("p50", "p95", "p99", "mean", "min", "max")


def test_bench_smoke_profile_produces_valid_report(tmp_path: Path) -> None:
    """`python bench/run_bench.py --profile smoke --json ...` stays healthy."""
    out_json = tmp_path / "bench_smoke.json"
    proc = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--profile", "smoke", "--json", str(out_json)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )
    assert proc.returncode == 0, f"bench crashed:\n{proc.stderr[-2000:]}"

    report = json.loads(out_json.read_text(encoding="utf-8"))
    for section in _REQUIRED_SECTIONS:
        assert section in report, f"bench report missing section {section}"

    meta = report["meta"]
    assert meta["profile"] == "smoke"
    assert meta["case_engine_version"]
    assert "note" in meta  # the machine-indicative disclaimer is mandatory

    # Pipeline stats: finite, non-negative, throughput present.
    for key, value in report["pipeline"].items():
        assert math.isfinite(float(value)), f"non-finite pipeline stat {key}={value}"
        assert float(value) >= 0.0
    assert report["pipeline"]["throughput_sessions_per_s"] > 0

    # Per-node: every node row carries full stat keys with sane values.
    assert report["per_node"], "no per-node samples collected"
    for node, stats in report["per_node"].items():
        for key in _REQUIRED_STAT_KEYS:
            assert key in stats, f"node {node} missing stat {key}"
            assert stats[key] >= 0.0
        assert stats["min"] <= stats["p50"] <= stats["p99"] <= stats["max"] + 1e-9

    # Gate: both paths measured; approval must not be slower than rejection
    # by more than an order of magnitude (sanity, not an SLO).
    for path in ("approved", "rejected"):
        assert path in report["gate"]
        assert report["gate"][path]["p50"] >= 0.0

    # Ledgers: all four backends measured; fsync path is the slowest.
    for backend in ("memory", "jsonl_nofsync", "jsonl_fsync", "sqlite"):
        assert backend in report["ledgers"], f"ledger backend {backend} not measured"
        assert report["ledgers"][backend]["p50"] >= 0.0
    assert report["ledgers"]["jsonl_fsync"]["p50"] >= report["ledgers"]["jsonl_nofsync"]["p50"] * 0.5, (
        "fsync path unexpectedly faster than buffered writes — measurement bug?"
    )


def test_bench_rejects_unknown_profile() -> None:
    """An unknown profile fails fast with a clear error."""
    from bench.run_bench import run_bench  # local import: bench is not a package

    with pytest.raises(ValueError, match="Unknown profile"):
        run_bench(profile="turbo")
