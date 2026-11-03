"""Example 1 — basic reasoning with the offline mock provider.

Run:  python examples/basic_reasoning.py

Demonstrates the minimal CASEEngine usage: zero configuration, mock
LLM, JSONL audit ledger, one full pipeline execution.
"""

from __future__ import annotations

from case_engine import CASEEngine


def main() -> None:
    """Run one query through the full deterministic pipeline."""
    engine = CASEEngine()  # mock provider + ./case_audit.jsonl ledger

    result = engine.run(
        "SycliQ Zone 4 soil sensors are reporting anomalous moisture spikes. "
        "Diagnose and plan the response."
    )

    print(result.final_response)
    print(
        f"\n[meta] domain={result.domain_classification} "
        f"models={result.selected_model_ids} retries={result.retry_count} "
        f"escalated={result.escalated} audit_events={result.audit_event_count}"
    )
    print(f"[meta] audit chain valid: {engine.verify_audit_chain().valid}")


if __name__ == "__main__":
    main()
