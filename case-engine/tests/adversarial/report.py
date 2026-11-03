"""Standalone replay runner for the injection corpus (G23).

Replays every :data:`tests.adversarial.corpus.INJECTION_CORPUS` payload
through a fully wired **offline** engine (mock provider, in-memory
ledger) and prints the verdict table, so the adversarial posture can be
reviewed without pytest:

    python -m tests.adversarial.report

Exit code ``0`` when every case satisfies the structural invariants
(defined terminal state, intact audit chain, PII policy honoured),
``1`` otherwise.  The runner never performs network I/O and never logs
prompt content above DEBUG — case ids and verdict flags only.

Cross-references: ``tests/adversarial/corpus.py``,
``tests/integration/test_adversarial_injection.py`` (the regression
variant of the same invariants).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from case_engine.engine import CASEEngine
from case_engine.llm import MockLLMClient
from case_engine.logging_setup import configure_logging, get_logger
from case_engine.registry import CognitiveRegistry
from case_engine.schemas.pipeline import CASEResult

from .corpus import INJECTION_CORPUS, InjectionCase, corpus_stats

__all__ = ["replay_case", "run_report", "main"]

log = get_logger(__name__)

#: Verdict columns printed per case (fixed width, single line).
_COLUMNS = ("case_id", "category", "terminal", "pii", "attempts", "chain_ok", "valid")


def _terminal_state(result: CASEResult) -> str:
    """Classify a result into its terminal-state label.

    Args:
        result: The engine outcome for one corpus case.

    Returns:
        One of ``completed``, ``escalated``, ``aborted``,
        ``passthrough``, ``degraded`` (failed but contained) — the last
        one meaning "no exception, no synthesis, session closed".
    """
    if result.all_valid:
        return "completed"
    if result.escalated:
        return "escalated"
    if result.aborted:
        return "aborted"
    if result.passthrough:
        return "passthrough"
    return "degraded"


def replay_case(engine: CASEEngine, case: InjectionCase) -> dict[str, Any]:
    """Run one corpus case and collect its verdict row.

    Structural invariants are *checked*, not asserted: the runner
    reports violations in the table and via the exit code instead of
    stopping at the first failure (a report that dies mid-way hides the
    remaining rows).

    Args:
        engine: Wired offline engine (mock provider + memory ledger).
        case: The corpus case to replay.

    Returns:
        A row dict with the :data:`_COLUMNS` keys plus ``chain_ok`` and
        a ``violations`` list of human-readable invariant breaches.
    """
    violations: list[str] = []
    try:
        result = engine.run(case.prompt)
    except Exception as exc:  # invariant 1: the pipeline must absorb manipulation
        log.error("corpus case raised", extra={"case_id": case.case_id, "error": str(exc)})
        return {
            "case_id": case.case_id, "category": case.category, "terminal": "RAISED",
            "pii": False, "attempts": -1, "chain_ok": False, "valid": False,
            "violations": [f"engine.run raised {type(exc).__name__}: {exc}"],
        }

    terminal = _terminal_state(result)
    if terminal == "degraded":
        violations.append("session ended without a defined terminal state")
    chain_ok = bool(engine.ledger.verify_chain().valid)
    if not chain_ok:
        violations.append("audit hash chain failed verification after run")
    if case.expect_pii_flag and not result.pii_flagged:
        violations.append("PII payload was not flagged (G1 policy bypass?)")
    if terminal == "completed" and case.expect_pii_flag:
        violations.append("PII payload was synthesized instead of escalated/aborted")

    attempts = max((t.attempts for t in result.traces), default=0)
    return {
        "case_id": case.case_id, "category": case.category, "terminal": terminal,
        "pii": bool(result.pii_flagged), "attempts": attempts, "chain_ok": chain_ok,
        "valid": not violations, "violations": violations,
    }


def run_report(engine: CASEEngine | None = None, quiet: bool = False) -> tuple[bool, list[dict[str, Any]]]:
    """Replay the whole corpus and print the verdict table.

    Args:
        engine: Optional pre-wired engine; a default offline engine
            (mock provider scripted from the registry, in-memory ledger)
            is built when omitted.
        quiet: Suppress the per-case table (used by tests that only need
            the boolean outcome).

    Returns:
        ``(all_clean, rows)`` — *all_clean* is True when no case violated
        any invariant.
    """
    if engine is None:
        from case_engine.config import EngineConfig

        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory"),
            llm_client=MockLLMClient.from_registry_examples(CognitiveRegistry.load_default()),
        )
    stats = corpus_stats()
    if not quiet:
        print(f"C.A.S.E. injection corpus — {stats['total']} cases across "
              f"{len(stats) - 1} categories")
        print("  " + "  ".join(f"{cat}={n}" for cat, n in stats.items() if cat != "total"))
        header = f"{'case':8} {'category':24} {'terminal':12} {'pii':4} {'try':4} {'chain':6} ok"
        print(header)
        print("-" * len(header))

    all_clean = True
    rows: list[dict[str, Any]] = []
    for case in INJECTION_CORPUS:
        row = replay_case(engine, case)
        rows.append(row)
        all_clean = all_clean and row["valid"]
        if not quiet:
            print(
                f"{row['case_id']:8} {row['category']:24} {row['terminal']:12} "
                f"{str(row['pii']):5} {row['attempts']:<4} {str(row['chain_ok']):6} "
                f"{'yes' if row['valid'] else 'NO'}"
            )
            for violation in row["violations"]:
                print(f"    ! {violation}")

    if not quiet:
        print("-" * len(_COLUMNS * 8))
        verdict = "ALL INVARIANTS HELD" if all_clean else "INVARIANT VIOLATIONS DETECTED"
        print(verdict)
    log.info("adversarial corpus replay finished", extra={"cases": len(rows), "clean": all_clean})
    return all_clean, rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python -m tests.adversarial.report``).

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` all invariants held, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m tests.adversarial.report",
        description="Replay the G23 prompt-injection corpus against an offline engine.",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Engine log verbosity (default: WARNING).",
    )
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level)
    try:
        all_clean, _rows = run_report()
    except Exception as exc:
        log.error("corpus replay crashed", extra={"error": str(exc)}, exc_info=True)
        print(f"REPLAY ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0 if all_clean else 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
