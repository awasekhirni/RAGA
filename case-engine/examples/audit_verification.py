"""Example 3 — tamper-evident audit trail.

Run:  python examples/audit_verification.py

Runs a session, verifies the hash chain, then silently edits one
historical event and shows ``verify_chain()`` catching the tampering.
"""

from __future__ import annotations

import json
from pathlib import Path

from case_engine import CASEEngine, EngineConfig
from case_engine.llm import MockLLMClient


def main() -> None:
    """Demonstrate end-to-end tamper evidence."""
    ledger_path = Path("audit_demo.jsonl")
    engine = CASEEngine(
        config=EngineConfig(ledger_backend="jsonl", ledger_path=str(ledger_path)),
        llm_client=MockLLMClient(),
    )
    engine.run("Zone 4 sensors report anomalous spikes")

    verification = engine.verify_audit_chain()
    print(f"after run:     valid={verification.valid} events={verification.total_entries}")

    # --- an attacker rewrites history in the ledger file -----------------
    lines = ledger_path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    record["payload"]["engine_version"] = "0.0.1"  # falsify provenance
    lines[0] = json.dumps(record)
    ledger_path.write_text("\n".join(lines) + "\n")

    verification = engine.verify_audit_chain()
    print(f"after tamper:  valid={verification.valid}")
    print(f"               reason={verification.reason}")
    assert not verification.valid, "tampering must be detected"


if __name__ == "__main__":
    main()
