"""Example 2 — the reflexion loop in action.

Run:  python examples/reflexion_demo.py

A scripted LLM misbehaves once (invalid tool arguments).  The engine
rejects the payload, feeds the validation errors back, regenerates, and
completes — the "LLM-as-generator, code-as-enforcer" separation.
"""

from __future__ import annotations

from case_engine import CASEEngine, EngineConfig, GlobalGuardrails
from case_engine.audit import InMemoryLedger
from case_engine.llm import MockLLMClient


def main() -> None:
    """Force one invalid generation and watch the engine recover."""
    misbehaving_llm = MockLLMClient().fail_next(times=1, mode="invalid_args")

    engine = CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
            ledger_backend="memory",
        ),
        llm_client=misbehaving_llm,
        ledger=InMemoryLedger(),
    )

    result = engine.run("Dashboard shows stale data")

    print(result.final_response)
    print(f"\n[meta] retries needed: {result.retry_count} (first attempt was rejected)")
    assert result.retry_count == 1, "the scripted failure must trigger one reflexion cycle"


if __name__ == "__main__":
    main()
