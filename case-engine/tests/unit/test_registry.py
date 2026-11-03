"""Unit tests: registry integrity, lookups and Cynefin routing."""

from __future__ import annotations

import pytest

from case_engine.exceptions import RegistryError, RoutingError
from case_engine.registry import CognitiveRegistry, MentalModelDefinition, ModelRouter
from case_engine.registry.models import ValidationRuleDefinition
from case_engine.schemas.mental_models import SCHEMA_BY_MODEL_ID


class TestRegistryIntegrity:
    """The shipped registry must be complete and self-consistent."""

    def test_fifteen_models_registered(self, registry: CognitiveRegistry) -> None:
        assert len(registry) == 15

    def test_sixteen_rules_registered(self, registry: CognitiveRegistry) -> None:
        assert len(registry.all_rules()) == 16

    def test_model_ids_are_contiguous(self, registry: CognitiveRegistry) -> None:
        ids = [m.algorithm_id for m in registry.all_models()]
        assert ids == [f"MM{i:03d}" for i in range(1, 16)]

    def test_rule_ids_are_contiguous(self, registry: CognitiveRegistry) -> None:
        ids = [r.rule_id for r in registry.all_rules()]
        assert ids == [f"VR{i:03d}" for i in range(1, 17)]

    def test_every_model_resolves_to_a_typed_contract(self, registry) -> None:
        for model in registry.all_models():
            cls = SCHEMA_BY_MODEL_ID.get(model.algorithm_id)
            assert cls is not None, model.algorithm_id
            assert cls.__name__ == model.schema_class

    def test_every_model_has_at_least_one_example_and_two_rules(self, registry) -> None:
        for model in registry.all_models():
            assert model.examples, f"{model.algorithm_id} lacks examples"
            assert len(model.validation_rules) >= 2, model.algorithm_id  # domain rule + VR016

    def test_vr016_is_universal(self, registry) -> None:
        rule = registry.get_rule("VR016")
        assert rule.applies_to == ["UNIVERSAL"]
        for model in registry.all_models():
            assert "VR016" in model.validation_rules


class TestLookups:
    """Registry lookup paths must resolve in both directions."""

    def test_get_by_id(self, registry) -> None:
        assert registry.get("MM005").algorithm_name == "Root Cause Analysis (5 Whys)"

    def test_get_by_tool_name(self, registry) -> None:
        assert registry.get_by_tool_name("RootCauseAnalysisTrace").algorithm_id == "MM005"

    def test_get_unknown_id_raises(self, registry) -> None:
        with pytest.raises(RegistryError):
            registry.get("MM999")

    def test_get_unknown_tool_raises(self, registry) -> None:
        with pytest.raises(RegistryError):
            registry.get_by_tool_name("NoSuchTool")

    def test_get_unknown_rule_raises(self, registry) -> None:
        with pytest.raises(RegistryError):
            registry.get_rule("VR999")


class TestConsistency:
    """Corrupt registry data must fail fast at construction."""

    def _models(self, registry):
        return [m.model_dump() for m in registry.all_models()]

    def _rules(self, registry):
        return [r.model_dump() for r in registry.all_rules()]

    def test_duplicate_model_id_rejected(self, registry) -> None:
        models = self._models(registry)
        models.append(dict(models[0]))
        with pytest.raises(RegistryError):
            CognitiveRegistry(
                models=[MentalModelDefinition.model_validate(m) for m in models],
                rules=[ValidationRuleDefinition.model_validate(r) for r in self._rules(registry)],
                routing_matrix={},
            )

    def test_unknown_rule_reference_rejected(self, registry) -> None:
        models = self._models(registry)
        models[0]["validation_rules"] = ["VR999"]
        with pytest.raises(RegistryError):
            CognitiveRegistry(
                models=[MentalModelDefinition.model_validate(m) for m in models],
                rules=[ValidationRuleDefinition.model_validate(r) for r in self._rules(registry)],
                routing_matrix={},
            )

    def test_unknown_routing_model_rejected(self, registry) -> None:
        with pytest.raises(RegistryError):
            CognitiveRegistry(
                models=[MentalModelDefinition.model_validate(m) for m in self._models(registry)],
                rules=[ValidationRuleDefinition.model_validate(r) for r in self._rules(registry)],
                routing_matrix={"Clear": ["MM999"]},
            )


class TestRouting:
    """node_2 routing: deterministic, affinity-ranked, override-capable."""

    def test_complex_top3_matches_spec(self, router: ModelRouter) -> None:
        selected = [m.algorithm_id for m in router.select("Complex", top_n=3)]
        assert selected == ["MM008", "MM011", "MM015"]

    def test_chaotic_prefers_ooda(self, router: ModelRouter) -> None:
        assert router.select("Chaotic", top_n=1)[0].algorithm_id == "MM001"

    def test_specialist_domain_routes(self, router: ModelRouter) -> None:
        selected = [m.algorithm_id for m in router.select("Legacy_Refactoring", top_n=1)]
        assert selected == ["MM010"]

    def test_forced_models_bypass_ranking(self, router: ModelRouter) -> None:
        selected = router.select("Complex", forced_model_ids=["MM005", "MM001"])
        assert [m.algorithm_id for m in selected] == ["MM005", "MM001"]

    def test_unknown_domain_raises(self, router: ModelRouter) -> None:
        with pytest.raises(RoutingError):
            router.select("Cynefin-Foo")

    def test_explain_returns_scores(self, router: ModelRouter) -> None:
        scores = router.explain("Complex")
        assert scores["MM008"] == 0.95
        assert scores["MM015"] == 0.9

    @pytest.fixture()
    def router(self, registry) -> ModelRouter:
        return ModelRouter(registry)
