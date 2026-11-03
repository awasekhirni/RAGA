"""Integration test: the optional LangGraph runner produces identical results.

Skipped automatically when ``langgraph`` is not installed
(``pip install "case-engine[langgraph]"``).
"""

from __future__ import annotations

import pytest

langgraph = pytest.importorskip("langgraph", reason="langgraph extra not installed")

from case_engine.audit import InMemoryLedger
from case_engine.config import EngineConfig
from case_engine.engine import CASEEngine
from case_engine.llm import MockLLMClient
from case_engine.pipeline.langgraph_adapter import run_case_graph
from case_engine.pipeline.state_machine import CASEStateMachine


def _initial_state(query: str, session_id: str) -> dict:
    return {
        "session_id": session_id, "user_query": query, "context": {},
        "domain_classification": None, "domain_source": None,
        "selected_model_ids": [], "forced_model_ids": None,
        "reasoning_traces": {}, "validation_errors": {}, "retry_count": 0,
        "all_valid": False, "final_response": None, "current_node": None,
        "next_node": None, "phase": None, "tool_specs": [],
        "error_detail": None, "escalated": False, "started_at": "",
        "finished_at": None,
    }


def test_langgraph_matches_pure_python_runner() -> None:
    mock = MockLLMClient()
    engine = CASEEngine(
        config=EngineConfig(ledger_backend="memory"),
        llm_client=mock, ledger=InMemoryLedger(),
    )
    state = _initial_state("Dashboard shows stale data", "lg-parity-1")

    via_machine = CASEStateMachine(engine.ctx).run(dict(state))
    via_langgraph = run_case_graph(engine.ctx, _initial_state("Dashboard shows stale data", "lg-parity-1"))

    assert via_langgraph["all_valid"] is True
    assert via_langgraph["domain_classification"] == via_machine["domain_classification"]
    assert via_langgraph["selected_model_ids"] == via_machine["selected_model_ids"]
    assert via_langgraph["escalated"] == via_machine["escalated"]
    assert engine.verify_audit_chain().valid
