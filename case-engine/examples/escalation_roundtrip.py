"""Escalation round trip (G6): sink delivery + human-in-the-loop resume.

Demonstrates the closed escalation loop added in v2.2.0:

1. Run a session scripted to exhaust its reflexion budget -> node_8
   packages an escalation and delivers it to a JSONL
   :class:`~case_engine.escalation.FileEscalationSink` (in addition to
   the always-present ``ESCALATED`` audit event).
2. Load the handoff back from the sink file (what a ticketing pipeline
   would receive) and feed a human verdict back in via
   :meth:`CASEEngine.resume` — a fresh, fully-audited session whose
   context carries the verdict as data.
3. Print both sessions' outcomes, the handoff JSON and the chain
   verification for both sessions' shared ledger.

Run: ``python examples/escalation_roundtrip.py``
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from case_engine import CASEEngine
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.escalation import FileEscalationSink, handoff_from_result
from case_engine.llm import MockLLMClient
from case_engine.logging_setup import configure_logging


def main() -> None:
    """Run the escalation -> human verdict -> resume round trip."""
    configure_logging(level="CRITICAL")  # keep the example output clean
    with tempfile.TemporaryDirectory() as tmp:
        sink_path = Path(tmp) / "escalations.jsonl"

        # 1) A session that exhausts reflexion and escalates.  The mock
        #    fails its first tool call and the reflexion budget is 0, so
        #    node_8 runs and delivers the handoff to the JSONL sink.
        failing = MockLLMClient(failure_plan=["no_tool_call"])
        engine = CASEEngine(
            config=EngineConfig(
                ledger_backend="memory",
                guardrails=GlobalGuardrails(max_reflexion_retries=0),
            ),
            llm_client=failing,
            escalation_sink=FileEscalationSink(sink_path),
        )
        prior = engine.run("Zone 4 sensors spike anomalously")
        print("=" * 72)
        print(f"session 1 escalated={prior.escalated} "
              f"retry_count={prior.retry_count}")
        print(prior.final_response.splitlines()[0])

        # 2) What the ticketing pipeline received (JSONL handoff file).
        handoff_line = sink_path.read_text(encoding="utf-8").strip().splitlines()[0]
        handoff_json = json.loads(handoff_line)
        print("-" * 72)
        print("handoff delivered to sink:")
        print(json.dumps({k: handoff_json[k] for k in
                          ("session_id", "reason", "selected_model_ids",
                           "retry_count")}, indent=2))

        # 3) The human verdict flows back in: resume() starts a fresh,
        #    fully-governed session seeded with the escalation evidence
        #    and the verdict as operator context data.
        sink_handoff = json.loads(sink_path.read_text(encoding="utf-8")
                                  .strip().splitlines()[0])
        from case_engine.escalation import EscalationHandoff

        handoff = EscalationHandoff.model_validate(sink_handoff)
        resumed = engine.resume(
            handoff_from_result(prior, package=handoff.model_dump()),
            reviewer_verdict="Approved: sensor data validated out-of-band; proceed.",
            reviewer_id="alice@example",
        )
        print("-" * 72)
        print(f"session 2 (resumed) escalated={resumed.escalated} "
              f"all_valid={resumed.all_valid}")
        print(resumed.final_response.splitlines()[0])

        # 4) Both sessions share one verifiable audit chain.
        verification = engine.verify_audit_chain()
        print("-" * 72)
        print(f"audit chain: valid={verification.valid} "
              f"events={verification.total_entries}")
        print("=" * 72)


if __name__ == "__main__":
    main()
