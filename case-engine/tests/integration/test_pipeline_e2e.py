"""Integration tests: end-to-end pipeline runs with the deterministic MockLLM."""

from __future__ import annotations

from copy import deepcopy

import pytest

from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import CASEError
from case_engine.llm import MockLLMClient
from case_engine.schemas.pipeline import AuditEventType


class TestHappyPath:
    def test_full_run_success(self, engine: CASEEngine) -> None:
        result = engine.run("SycliQ Zone 4 soil sensors reporting anomalous spikes")
        assert result.all_valid and not result.escalated
        assert result.domain_classification == "Complex"
        assert result.selected_model_ids == ["MM008", "MM011", "MM015"]
        assert result.retry_count == 0
        assert len(result.traces) == 4  # classifier + 3 routed models
        for trace in result.traces:
            assert trace.report is not None and trace.report.passed

    def test_final_response_mentions_every_model(self, engine) -> None:
        result = engine.run("SycliQ Zone 4 soil sensors reporting anomalous spikes")
        for model_id in result.selected_model_ids:
            assert f"[{model_id}]" in (result.final_response or "")

    def test_audit_chain_verifies_after_run(self, engine) -> None:
        engine.run("Dashboard shows stale data")
        verification = engine.verify_audit_chain()
        assert verification.valid and verification.total_entries >= 10

    def test_audit_event_sequence(self, engine, ledger) -> None:
        engine.run("Dashboard shows stale data")
        types = [e.event_type for e in ledger.iter_events()]
        assert types[0] == AuditEventType.SESSION_START.value
        assert AuditEventType.DOMAIN_CLASSIFIED.value in types
        assert AuditEventType.MODELS_SELECTED.value in types
        assert AuditEventType.SCHEMAS_INJECTED.value in types
        assert types[-1] == AuditEventType.SESSION_COMPLETED.value


class TestRoutingControls:
    def test_domain_preset_skips_classifier_llm(self, engine) -> None:
        result = engine.run("anything at all", domain_hint="Chaotic")
        assert result.domain_classification == "Chaotic"
        assert result.domain_source == "preset"
        # Affinity ranking for Chaotic: MM001=0.95 > MM009=0.85 > MM007=0.8.
        assert [t.model_id for t in result.traces] == ["MM001", "MM009", "MM007"]
        assert result.all_valid

    def test_forced_models_override_router(self, engine) -> None:
        result = engine.run("Audit this legacy pricing module", forced_model_ids=["MM010"])
        assert result.selected_model_ids == ["MM010"]
        assert result.all_valid

    def test_unknown_domain_hint_escalates(self, engine) -> None:
        result = engine.run("q", domain_hint="Bureaucratic")
        assert result.escalated

    def test_specialist_domain_routes_legacy(self, engine) -> None:
        result = engine.run("q", domain_hint="Legacy_Refactoring")
        assert result.selected_model_ids == ["MM010", "MM012", "MM005"]


class TestReflexionLoop:
    def test_gate_rejection_then_success(self, registry) -> None:
        mock = MockLLMClient.from_registry_examples(registry).fail_next(
            times=1, mode="invalid_args"
        )
        engine = CASEEngine(
            config=EngineConfig(guardrails=GlobalGuardrails(max_reflexion_retries=2),
                                ledger_backend="memory"),
            llm_client=mock,
        )
        result = engine.run("Dashboard shows stale data")
        assert result.all_valid and result.retry_count == 1 and not result.escalated
        assert engine.verify_audit_chain().valid

    def test_rule_failure_triggers_regeneration(self, registry, gold_payloads) -> None:
        # First MM005 call returns a VR005-violating (human-blame) payload;
        # the second returns the gold example -> reflexion regenerates & passes.
        gold = deepcopy(gold_payloads["MM005"])
        poisoned = deepcopy(gold)
        poisoned["validated_root_cause"] = "The operator forgot to check the queue"

        from case_engine.llm import ToolCallResult

        payloads = iter([poisoned, gold])

        class SwitchingClient(MockLLMClient):
            """Serves the poisoned payload once, then the gold one."""

            def generate_tool_call(self, request):
                if request.primary_tool_name == "RootCauseAnalysisTrace":
                    return ToolCallResult(
                        tool_name="RootCauseAnalysisTrace",
                        arguments=next(payloads),
                        provider="mock",
                    )
                return super().generate_tool_call(request)

        engine = CASEEngine(
            config=EngineConfig(guardrails=GlobalGuardrails(max_reflexion_retries=2),
                                ledger_backend="memory"),
            llm_client=SwitchingClient(),
        )
        result = engine.run("Dashboard shows stale data", forced_model_ids=["MM005"])
        assert result.all_valid and result.retry_count == 1
        mm005 = next(t for t in result.traces if t.model_id == "MM005")
        assert mm005.attempts == 2
        assert "alerting" in mm005.payload["validated_root_cause"].lower()


class TestEscalation:
    def test_exhausted_reflexion_escalates(self, registry) -> None:
        mock = MockLLMClient.from_registry_examples(registry).fail_next(
            times=99, mode="invalid_args"
        )
        engine = CASEEngine(
            config=EngineConfig(guardrails=GlobalGuardrails(max_reflexion_retries=1),
                                ledger_backend="memory"),
            llm_client=mock,
        )
        result = engine.run("Dashboard shows stale data")
        assert result.escalated and not result.all_valid
        assert "escalated" in (result.final_response or "").lower()
        types = [e.event_type for e in engine.ledger.iter_events()]
        assert AuditEventType.ESCALATED.value in types
        package = next(
            e.payload for e in engine.ledger.iter_events()
            if e.event_type == AuditEventType.ESCALATED.value
        )
        assert package["user_query"] == "Dashboard shows stale data"

    def test_no_tool_call_failure_escalates(self, registry) -> None:
        mock = MockLLMClient.from_registry_examples(registry).fail_next(
            times=99, mode="no_tool_call"
        )
        engine = CASEEngine(
            config=EngineConfig(guardrails=GlobalGuardrails(max_reflexion_retries=1),
                                ledger_backend="memory"),
            llm_client=mock,
        )
        assert engine.run("q").escalated


class TestGuardrails:
    def test_empty_query_rejected(self, engine) -> None:
        with pytest.raises(CASEError):
            engine.run("   ")

    def test_passthrough_mode_flags_all_valid(self, registry) -> None:
        engine = CASEEngine(
            config=EngineConfig(
                guardrails=GlobalGuardrails(allow_unvalidated_passthrough=True),
                ledger_backend="memory",
            ),
            llm_client=MockLLMClient.from_registry_examples(registry),
        )
        result = engine.run("q")
        assert result.all_valid  # bypassed checks are flagged loudly in logs

    def test_result_counts_audit_events(self, engine) -> None:
        result = engine.run("q")
        assert result.audit_event_count >= 10
        assert result.last_audit_hash and len(result.last_audit_hash) == 64
