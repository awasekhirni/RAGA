"""Unit tests: enforcement gate + tool-spec factory."""

from __future__ import annotations

from copy import deepcopy

import pytest

from case_engine.enforcement import SchemaGate, ToolFactory
from case_engine.exceptions import RegistryError, ToolCallRejectedError
from case_engine.registry import CognitiveRegistry, MentalModelDefinition


@pytest.fixture()
def gate(registry) -> SchemaGate:
    return SchemaGate(registry)


@pytest.fixture()
def factory() -> ToolFactory:
    return ToolFactory()


class TestSchemaGate:
    """Strategy 1: every tool call re-validated server-side."""

    def test_approves_valid_payload(self, gate, gold_payloads) -> None:
        decision = gate.check("RootCauseAnalysisTrace", deepcopy(gold_payloads["MM005"]))
        assert decision.approved
        assert decision.parsed["validated_root_cause"]

    def test_rejects_missing_fields(self, gate) -> None:
        decision = gate.check("RootCauseAnalysisTrace", {"problem_statement": "x"})
        assert not decision.approved
        assert any("why_1" in e or "Field required" in e for e in decision.errors)

    def test_rejects_extra_fields(self, gate, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM003"])
        payload["hallucinated_field"] = True
        assert not gate.check("SixHatsEvaluation", payload).approved

    def test_rejects_bad_confidence(self, gate, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM004"])
        payload["confidence"] = 7.0
        assert not gate.check("ParetoOptimizationPlan", payload).approved

    def test_unknown_tool_is_enforcement_fault(self, gate) -> None:
        with pytest.raises(ToolCallRejectedError):
            gate.check("DefinitelyNotARealTool", {})


class TestToolFactory:
    """Provider spec generation from typed contracts."""

    def test_openai_tool_shape(self, factory, registry) -> None:
        spec = factory.openai_tool(registry.get("MM005"))
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] == "RootCauseAnalysisTrace"
        assert fn["parameters"]["type"] == "object"
        assert "confidence" in fn["parameters"]["properties"]
        assert fn["description"]  # description_for_llm carried over

    def test_anthropic_tool_shape(self, factory, registry) -> None:
        spec = factory.anthropic_tool(registry.get("MM013"))
        assert spec["name"] == "ComplexityDomainClassifier"
        assert spec["input_schema"]["type"] == "object"
        assert "assigned_domain" in spec["input_schema"]["properties"]

    def test_json_schema_is_deterministic(self, factory, registry) -> None:
        s1 = factory.json_schema(registry.get("MM001"))
        s2 = factory.json_schema(registry.get("MM001"))
        assert s1 == s2

    def test_schema_class_mismatch_detected(self, factory) -> None:
        bad = MentalModelDefinition.model_validate(
            {**_mm001_raw(), "schema_class": "TotallyWrongName"}
        )
        with pytest.raises(RegistryError):
            factory.openai_tool(bad)


def _mm001_raw() -> dict:
    registry = CognitiveRegistry.load_default()
    return registry.get("MM001").model_dump()
