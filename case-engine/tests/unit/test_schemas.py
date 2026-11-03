"""Unit tests: the 15 typed mental-model contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from case_engine.schemas.mental_models import SCHEMA_BY_MODEL_ID


def _contract_cases(registry):
    """Yield (model_id, contract_class, gold_example) for every model."""
    for model in registry.all_models():
        cls = SCHEMA_BY_MODEL_ID[model.algorithm_id]
        yield model.algorithm_id, cls, model.examples[0]


class TestGoldExamplesValidate:
    """Every registry gold example must parse against its typed contract."""

    def test_all_gold_examples(self, registry) -> None:
        failures = []
        for model_id, cls, example in _contract_cases(registry):
            try:
                cls.model_validate(deepcopy(example))
            except ValidationError as exc:
                failures.append(f"{model_id}: {exc.errors(include_url=False)[:2]}")
        assert not failures, failures


class TestStrictness:
    """Contracts are strict: extra fields, bad ranges and bad literals fail."""

    def test_extra_field_rejected(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM001"])
        payload["sneaky_extra"] = "nope"
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM001"].model_validate(payload)

    def test_missing_confidence_rejected(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM002"])
        payload.pop("confidence")
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM002"].model_validate(payload)

    @pytest.mark.parametrize("confidence", [-0.1, 1.01, 2.0])
    def test_confidence_bounds(self, gold_payloads, confidence) -> None:
        payload = deepcopy(gold_payloads["MM003"])
        payload["confidence"] = confidence
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM003"].model_validate(payload)

    def test_mm007_minute_cap(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM007"])
        payload["emotional_budget_allocated_minutes"] = 6
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM007"].model_validate(payload)

    def test_mm013_domain_literal(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM013"])
        payload["assigned_domain"] = "Bureaucratic"
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM013"].model_validate(payload)

    def test_mm015_threshold_bounds(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM015"])
        payload["drift_detection_threshold"] = 1.5
        with pytest.raises(ValidationError):
            SCHEMA_BY_MODEL_ID["MM015"].model_validate(payload)


class TestDefaults:
    """Timestamps are auto-filled and confidence is mandatory."""

    def test_timestamp_autofilled(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM005"])
        payload.pop("timestamp", None)
        parsed = SCHEMA_BY_MODEL_ID["MM005"].model_validate(payload)
        assert parsed.timestamp.startswith("2")  # ISO year, tz-aware

    def test_string_stripping(self, gold_payloads) -> None:
        payload = deepcopy(gold_payloads["MM005"])
        payload["why_1"] = "  padded answer  "
        parsed = SCHEMA_BY_MODEL_ID["MM005"].model_validate(payload)
        assert parsed.why_1 == "padded answer"
