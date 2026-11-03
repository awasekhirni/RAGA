"""Shared pytest fixtures for the C.A.S.E. test suite."""

from __future__ import annotations

from copy import deepcopy

import pytest

from case_engine.audit import InMemoryLedger
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.llm import MockLLMClient
from case_engine.registry import CognitiveRegistry


@pytest.fixture(scope="session")
def registry() -> CognitiveRegistry:
    """Session-scoped default cognitive registry (15 models, 16 rules)."""
    return CognitiveRegistry.load_default()


@pytest.fixture()
def guardrails() -> GlobalGuardrails:
    """Per-test guardrails with a small reflexion budget for speed."""
    return GlobalGuardrails(max_reflexion_retries=2)


@pytest.fixture()
def mock_client(registry: CognitiveRegistry) -> MockLLMClient:
    """Mock LLM scripted with every registry gold example."""
    return MockLLMClient.from_registry_examples(registry)


@pytest.fixture()
def ledger() -> InMemoryLedger:
    """Fresh in-memory audit ledger per test."""
    return InMemoryLedger()


@pytest.fixture()
def engine(mock_client: MockLLMClient, ledger: InMemoryLedger, guardrails: GlobalGuardrails) -> CASEEngine:
    """Fully wired offline engine (mock provider + memory ledger)."""
    return CASEEngine(
        config=EngineConfig(guardrails=guardrails, ledger_backend="memory"),
        llm_client=mock_client,
        ledger=ledger,
    )


@pytest.fixture()
def gold_payloads(registry: CognitiveRegistry) -> dict[str, dict]:
    """Deep copies of every model's gold example, keyed by algorithm id."""
    return {
        m.algorithm_id: deepcopy(m.examples[0]) for m in registry.all_models() if m.examples
    }
