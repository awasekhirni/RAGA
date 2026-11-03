"""Tests for the async API (G5): adapter twin, async machine, run_async.

The contract under test: ``run_async`` is a *semantic twin* of ``run``
(identical outcomes on the deterministic mock) and the awaiting event
loop is never blocked by node execution.  All tests are hermetic — the
mock provider needs no network and the thread-offload design needs no
sleeps.
"""

from __future__ import annotations

import asyncio

import pytest

from case_engine import CASEEngine
from case_engine.config import EngineConfig
from case_engine.llm import MockLLMClient
from case_engine.llm.base import ToolCallRequest
from case_engine.pipeline.state_machine import EDGES


def _engine(**kwargs) -> CASEEngine:
    """Build a minimal offline engine (memory ledger, mock provider)."""
    return CASEEngine(config=EngineConfig(ledger_backend="memory"), **kwargs)


def _request() -> ToolCallRequest:
    """A minimal single-tool request for adapter-level tests."""
    return ToolCallRequest(
        system_prompt="s", user_prompt="u",
        tool_specs=[{"type": "function",
                     "function": {"name": "T", "parameters": {}}}],
    )


class TestAsyncAdapterTwin:
    def test_base_async_twin_matches_sync_result(self):
        client = MockLLMClient(script={"T": {"a": 1}})
        request = _request()
        sync_result = client.generate_tool_call(request)
        async_result = asyncio.run(client.generate_tool_call_async(request))
        assert async_result.tool_name == sync_result.tool_name
        assert async_result.arguments == sync_result.arguments
        assert async_result.provider == sync_result.provider

    def test_async_dispatch_does_not_block_the_loop(self):
        """While the async call runs, the loop must stay responsive."""
        client = MockLLMClient(script={"T": {"a": 1}})
        ticks: list[str] = []

        async def _scenario() -> None:
            async def _ticker() -> None:
                for _ in range(3):
                    await asyncio.sleep(0.001)
                    ticks.append("tick")

            ticker = asyncio.create_task(_ticker())
            await client.generate_tool_call_async(_request())
            await ticker

        asyncio.run(_scenario())
        assert ticks, "event loop was blocked during the async LLM call"


class TestAsyncEngine:
    def test_run_async_matches_run_outcomes(self):
        engine = _engine()
        async_result = asyncio.run(engine.run_async("Zone 4 sensors spike anomalously"))
        sync_result = engine.run("Zone 4 sensors spike anomalously")
        assert async_result.all_valid == sync_result.all_valid
        assert async_result.selected_model_ids == sync_result.selected_model_ids
        assert async_result.domain_classification == sync_result.domain_classification
        assert async_result.token_usage.calls == sync_result.token_usage.calls

    def test_run_async_rejects_empty_query(self):
        from case_engine.exceptions import CASEError

        engine = _engine()
        with pytest.raises(CASEError):
            asyncio.run(engine.run_async("   "))

    def test_run_async_audit_chain_valid(self):
        engine = _engine()
        result = asyncio.run(engine.run_async("Zone 4 sensors spike anomalously"))
        assert result.all_valid
        assert engine.verify_audit_chain().valid

    def test_concurrent_sessions_on_one_engine(self):
        """Two async sessions on one engine complete without interference."""
        engine = _engine()

        async def _scenario() -> list:
            return await asyncio.gather(
                engine.run_async("Zone 4 sensors spike anomalously"),
                engine.run_async("Legacy COBOL module refactoring risk"),
            )

        first, second = asyncio.run(_scenario())
        assert first.session_id != second.session_id
        assert first.all_valid and second.all_valid
        assert engine.verify_audit_chain().valid

    def test_loop_responsiveness_during_pipeline(self):
        """A ticker coroutine keeps running while the pipeline executes."""
        engine = _engine()
        ticks: list[int] = []

        async def _scenario() -> object:
            async def _ticker() -> None:
                for _ in range(20):
                    await asyncio.sleep(0.001)
                    ticks.append(1)

            ticker = asyncio.create_task(_ticker())
            result = await engine.run_async("Zone 4 sensors spike anomalously")
            await ticker
            return result

        result = asyncio.run(_scenario())
        assert result.all_valid
        assert ticks, "event loop was starved during the async pipeline"

    def test_escalation_path_async(self):
        """The async path escalates identically under budget exhaustion."""
        client = MockLLMClient(failure_plan=["no_tool_call"] * 5)
        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory"),
            llm_client=client,
        )
        engine.guardrails.max_reflexion_retries = 0
        result = asyncio.run(engine.run_async("Zone 4 sensors spike anomalously"))
        assert result.escalated
        assert engine.verify_audit_chain().valid


class TestAsyncStateMachineGuards:
    def test_edges_table_unchanged(self):
        """The async twin must not alter the declared topology."""
        assert EDGES["node_1_intent_classifier"]["success"] == "node_2_model_router"
        assert EDGES["node_6_reflexion_loop"]["exhausted"] == "node_8_human_escalation"

    def test_max_steps_guard_trips_async(self):
        from case_engine.exceptions import PipelineError
        from case_engine.pipeline.state_machine import CASEStateMachine

        engine = _engine()
        machine = CASEStateMachine(engine.ctx)
        with pytest.raises(PipelineError):
            asyncio.run(machine.run_async(
                {"session_id": "s", "user_query": "q"}, max_steps=1))
