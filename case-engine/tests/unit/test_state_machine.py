"""Unit tests: topology enforcement and state-machine guards."""

from __future__ import annotations

import pytest

from case_engine.config import EngineConfig
from case_engine.engine import CASEEngine
from case_engine.exceptions import PipelineError, TopologyViolationError
from case_engine.pipeline.nodes import NODES
from case_engine.pipeline.state_machine import EDGES, CASEStateMachine


class TestTopologyDeclaration:
    """The declared edge table must cover all nodes and reach END."""

    def test_all_nodes_declared(self) -> None:
        assert set(EDGES) == set(NODES)

    def test_all_destinations_are_known_nodes_or_end(self) -> None:
        known = set(NODES) | {"END"}
        for edges in EDGES.values():
            assert set(edges.values()) <= known

    def test_entry_has_no_incoming_from_terminals(self) -> None:
        # Terminal nodes must not loop back (guarantees termination shape).
        for node_id in ("node_7_audit_and_synthesis", "node_8_human_escalation"):
            assert set(EDGES[node_id].values()) == {"END"}

    def test_reflexion_can_reach_classify_generate_and_synthesis(self) -> None:
        # G2 added the "passthrough" edge (node_6 -> node_7): under
        # fallback_behavior="passthrough_with_flags" the failed traces
        # proceed to synthesis, loudly flagged (FALLBACK_APPLIED event).
        assert set(EDGES["node_6_reflexion_loop"].values()) == {
            "node_4_llm_generation", "node_1_intent_classifier",
            "node_8_human_escalation", "node_7_audit_and_synthesis",
        }


class TestTopologyEnforcement:
    """An illegal transition is a hard engine bug, not a soft warning."""

    def test_illegal_transition_raises(self, engine: CASEEngine, monkeypatch) -> None:
        def rogue_node(state, ctx):  # node_1 trying to teleport to node_7
            return "node_7_audit_and_synthesis", dict(state)

        monkeypatch.setitem(NODES, "node_1_intent_classifier", rogue_node)
        machine = CASEStateMachine(engine.ctx)
        state = {
            "session_id": "s-test", "user_query": "q", "reasoning_traces": {},
            "validation_errors": {}, "retry_count": 0, "tool_specs": [],
        }
        with pytest.raises(TopologyViolationError):
            machine.run(state)

    def test_max_steps_guard(self, monkeypatch, registry) -> None:
        def loop_forever(state, ctx):
            return "node_2_model_router", dict(state)  # illegal but self-loop-ish

        monkeypatch.setitem(NODES, "node_1_intent_classifier", loop_forever)
        # Give the rogue edge legality by declaring it (simulates a bug).
        monkeypatch.setitem(EDGES, "node_1_intent_classifier",
                            {"success": "node_2_model_router"})
        monkeypatch.setitem(EDGES, "node_2_model_router",
                            {"success": "node_1_intent_classifier"})
        from case_engine.audit import InMemoryLedger
        from case_engine.llm import MockLLMClient
        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory"),
            llm_client=MockLLMClient(), ledger=InMemoryLedger(),
        )
        machine = CASEStateMachine(engine.ctx)
        state = {"session_id": "s-loop", "user_query": "q"}
        with pytest.raises(PipelineError):
            machine.run(state, max_steps=10)
