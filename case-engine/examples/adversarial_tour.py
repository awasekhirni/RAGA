"""Example 6 — the adversarial tour: every failure mode, end to end.

Run:  python examples/adversarial_tour.py

The constitution's promise is that a *misbehaving* LLM can never produce
an unaudited, unvalidated or silent outcome.  This example tours the
full adversarial surface with the deterministic mock provider (zero
network):

1. **Hallucinated tool** — the model invents a tool the registry never
   declares; the enforcement gate hard-rejects it (fail-closed), so no
   unregistered reasoning path can ever execute.
2. **Schema-violating arguments** — well-named tool, invalid payload;
   Pydantic rejects it and reflexion regenerates.
3. **No tool call at all** — the model chats instead of calling the
   declared tool; reflexion regenerates.
4. **Persistent misbehavior** — failures exceed the reflexion budget;
   the session escalates to a typed human handoff instead of degrading
   silently.
5. **Tamper evidence** — the produced audit ledger is hash-verified,
   then a payload is mutated and the tampering is detected.

Every stage prints the observable outcome so the tour doubles as a
testimonial of the "LLM-as-generator, code-as-enforcer" separation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from case_engine import CASEEngine, EngineConfig, GlobalGuardrails
from case_engine.audit import JSONLLedger
from case_engine.exceptions import ToolCallRejectedError
from case_engine.llm import MockLLMClient
from case_engine.logging_setup import configure_logging


def _engine(llm: MockLLMClient, ledger: JSONLLedger) -> CASEEngine:
    """Build a tour engine: mock provider, 2 reflexion retries, JSONL ledger."""
    return CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
            ledger_backend="jsonl",
        ),
        llm_client=llm,
        ledger=ledger,
    )


def _banner(title: str, ok: bool, detail: str) -> None:
    """Print one tour stage outcome (kept assert-free for readability)."""
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {title}: {detail}")

def stage_1() -> None:
    """Stage 1: a hallucinated tool is hard-rejected (fail-closed)."""
    llm = MockLLMClient().fail_next(1, "unknown_tool")
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine(llm, JSONLLedger(Path(tmp) / "hallucinated.jsonl"))
        try:
            engine.run("Dashboard shows stale data")
        except ToolCallRejectedError as exc:
            _banner(
                "stage 1 · hallucinated tool",
                True,
                f"hard-rejected with {type(exc).__name__} — nothing was "
                f"silently accepted and the audit ledger holds the evidence",
            )
        else:  # pragma: no cover - the mock guarantees the rejection
            _banner("stage 1 · hallucinated tool", False, "was NOT rejected")


def stage_2_to_3() -> None:
    """Stages 2-3: single-shot payload failures that reflexion absorbs."""
    scripted = [
        ("invalid arguments", MockLLMClient().fail_next(1, "invalid_args")),
        ("no tool call", MockLLMClient().fail_next(1, "no_tool_call")),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for title, llm in scripted:
            ledger = JSONLLedger(Path(tmp) / f"{title.replace(' ', '_')}.jsonl")
            result = _engine(llm, ledger).run("Dashboard shows stale data")
            _banner(
                f"stage 2-3 · {title}",
                result.all_valid and result.retry_count == 1,
                f"recovered in {result.retry_count} reflexion cycle(s), "
                f"{result.audit_event_count} audit events, valid={result.all_valid}",
            )


def stage_4() -> None:
    """Stage 4: persistent misbehavior escalates instead of degrading."""
    llm = MockLLMClient().fail_next(99, "invalid_args")  # never recovers
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine(llm, JSONLLedger(Path(tmp) / "escalated.jsonl"))
        result = engine.run("Dashboard shows stale data")
    _banner(
        "stage 4 · persistent misbehavior",
        result.escalated and not result.all_valid,
        f"escalated={result.escalated}, retries={result.retry_count} "
        f"(budget exhausted) — handed to a human, never silently accepted",
    )


def stage_5() -> None:
    """Stage 5: the audit ledger detects payload tampering."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        ledger = JSONLLedger(path)
        llm = MockLLMClient().fail_next(1, "invalid_args")  # one reflexion
        _engine(llm, ledger).run("Dashboard shows stale data")

        clean = ledger.verify_chain()
        _banner(
            "stage 5a · chain verification", clean.valid,
            f"{clean.total_entries} entries verified, chain intact",
        )

        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"]["tampered"] = True
        lines[0] = json.dumps(event, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered = ledger.verify_chain()
        _banner(
            "stage 5b · tamper detection", not tampered.valid,
            f"mutation detected at seq={tampered.first_invalid_seq}: {tampered.reason}",
        )


def main() -> None:
    """Run the five adversarial stages in order."""
    configure_logging(level="CRITICAL")  # keep the example output clean
    print("C.A.S.E. adversarial tour — every stage is fully offline\n")
    stage_1()
    stage_2_to_3()
    stage_4()
    stage_5()
    print("\nTour complete: misbehavior was rejected, recovered, escalated, "
          "and the evidence trail caught the tampering.")


if __name__ == "__main__":
    main()
