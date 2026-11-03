"""P0 gap-closure tests (G1-G4): TEE/PII gate, fallback policies, judge deadline,
audit redaction + encryption.

Each test class maps to one gap in ``docs/GAP_ANALYSIS.md``; names carry the
gap id so a failure points straight at the requirement.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

from case_engine.audit import InMemoryLedger, JSONLLedger, SQLiteLedger, create_ledger
from case_engine.audit.base import GENESIS_HASH
from case_engine.audit.crypto import AEADPayloadCipher, load_key
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import LedgerError
from case_engine.llm import MockLLMClient
from case_engine.privacy import (
    NoopPIIDetector,
    NoopRedactor,
    PIIFinding,
    PIIRedactor,
    RegexPIIDetector,
)
from case_engine.schemas.pipeline import AuditEventType
from case_engine.validation.llm_judge import LLMBackedJudge

PII_QUERY = "Contact jane.doe@corp.com or call +1 (555) 123-4567 about the outage"
CLEAN_QUERY = "Zone 4 sensors spike anomalously"


def _engine(**cfg_kwargs) -> CASEEngine:
    """Build a fully offline engine with memory ledger and given config kwargs."""
    return CASEEngine(
        config=EngineConfig(ledger_backend="memory", **cfg_kwargs),
        llm_client=MockLLMClient.from_registry_examples(
            __import__("case_engine.registry", fromlist=["CognitiveRegistry"])
            .CognitiveRegistry.load_default()
        ),
        ledger=InMemoryLedger(),
    )


def _events(ledger: InMemoryLedger) -> list:
    """List all events of an in-memory ledger."""
    return list(ledger.iter_events())


# --------------------------------------------------------------------------- #
# G1 - PII/TEE gate
# --------------------------------------------------------------------------- #
class TestG1TeeGate:
    """PII without attestation follows pii_policy; attested sessions pass."""

    def test_escalate_policy_refuses_before_generation(self) -> None:
        engine = _engine(guardrails=GlobalGuardrails(max_reflexion_retries=1))
        result = engine.run(PII_QUERY)
        assert result.escalated is True
        assert result.pii_flagged is True
        assert result.tee_decision == "escalated"
        assert "email" in result.pii_categories
        assert "phone" in result.pii_categories
        # Refusal happened BEFORE routing: no models were selected.
        assert result.selected_model_ids == []
        types = [e.event_type for e in _events(engine.ledger)]
        assert AuditEventType.TEE_DECISION.value in types
        assert AuditEventType.ESCALATED.value in types
        # The refusal message names categories, never raw values.
        assert "jane.doe@corp.com" not in (result.final_response or "")

    def test_allow_with_flags_proceeds_and_flags(self) -> None:
        engine = _engine(
            guardrails=GlobalGuardrails(pii_policy="allow_with_flags",
                                        max_reflexion_retries=1),
        )
        result = engine.run(PII_QUERY)
        assert result.all_valid is True
        assert result.pii_flagged is True
        assert result.tee_decision == "allow_with_flags"
        assert result.selected_model_ids  # generation actually happened

    def test_tee_attestation_allows_session(self) -> None:
        engine = _engine(guardrails=GlobalGuardrails(max_reflexion_retries=1))
        result = engine.run(PII_QUERY, tee_attested=True)
        assert result.all_valid is True
        assert result.tee_decision == "tee_attested"

    def test_disabled_guardrail_records_policy_disabled(self) -> None:
        engine = _engine(
            guardrails=GlobalGuardrails(tee_required_for_pii=False,
                                        max_reflexion_retries=1),
        )
        result = engine.run(PII_QUERY)
        assert result.all_valid is True
        assert result.tee_decision == "policy_disabled"

    def test_clean_query_emits_no_tee_event(self) -> None:
        engine = _engine(guardrails=GlobalGuardrails(max_reflexion_retries=1))
        result = engine.run(CLEAN_QUERY)
        assert result.all_valid is True
        assert result.pii_flagged is False
        assert result.tee_decision is None
        types = [e.event_type for e in _events(engine.ledger)]
        assert AuditEventType.TEE_DECISION.value not in types

    def test_pii_in_context_is_detected(self) -> None:
        engine = _engine(guardrails=GlobalGuardrails(max_reflexion_retries=1))
        result = engine.run(CLEAN_QUERY, context={"owner": "bob@corp.com"})
        assert result.pii_flagged is True
        assert result.tee_decision == "escalated"


class TestPIIDetectors:
    """Unit coverage of the regex detector and noop variants."""

    def test_detector_categories(self) -> None:
        det = RegexPIIDetector()
        assert det.detect("mail me at a.b@x.io").detected
        assert det.detect("SSN 123-45-6789").categories == ["ssn"]
        assert det.detect("ip 10.0.0.7").categories == ["ip_address"]
        # Luhn-valid card (test number) is caught; invalid digit runs are not.
        assert "credit_card" in det.detect("card 4111 1111 1111 1111").categories
        assert det.detect("build 1234 5678 9012").detected is False
        assert det.detect(CLEAN_QUERY).detected is False

    def test_noop_detector(self) -> None:
        assert NoopPIIDetector().detect(PII_QUERY).detected is False

    def test_finding_rejects_extra_keys(self) -> None:
        with pytest.raises(Exception):
            PIIFinding(detected=False, sneaky="x")


# --------------------------------------------------------------------------- #
# G2 - fallback_behavior policies
# --------------------------------------------------------------------------- #
class TestG2FallbackPolicies:
    """Budget exhaustion honors the configured policy; always audited."""

    def _exhausting_engine(self, behavior: str) -> CASEEngine:
        engine = _engine(
            guardrails=GlobalGuardrails(max_reflexion_retries=0,
                                        fallback_behavior=behavior),
        )
        engine.llm_client.fail_next(times=1, mode="invalid_args")
        return engine

    def test_default_policy_escalates(self) -> None:
        engine = self._exhausting_engine("escalate_to_human_with_partial_trace")
        result = engine.run(CLEAN_QUERY)
        assert result.escalated is True
        assert result.aborted is False
        assert result.passthrough is False

    def test_abort_session_closes_without_escalation(self) -> None:
        engine = self._exhausting_engine("abort_session")
        result = engine.run(CLEAN_QUERY)
        assert result.aborted is True
        assert result.escalated is False
        assert result.all_valid is False
        types = [e.event_type for e in _events(engine.ledger)]
        assert AuditEventType.FALLBACK_APPLIED.value in types
        assert "aborted" in (result.final_response or "").lower()

    def test_passthrough_with_flags_proceeds_flagged(self) -> None:
        engine = self._exhausting_engine("passthrough_with_flags")
        result = engine.run(CLEAN_QUERY)
        assert result.passthrough is True
        assert result.all_valid is False  # NOT a validated answer
        assert result.escalated is False
        assert "PASSED-THROUGH" in (result.final_response or "") or \
               "WARNING" in (result.final_response or "")
        types = [e.event_type for e in _events(engine.ledger)]
        assert AuditEventType.FALLBACK_APPLIED.value in types
        assert types[-1] == AuditEventType.SESSION_COMPLETED.value

    def test_invalid_policy_rejected_at_config(self) -> None:
        with pytest.raises(Exception):
            GlobalGuardrails(fallback_behavior="wing_it")


# --------------------------------------------------------------------------- #
# G3 - judge hard deadline
# --------------------------------------------------------------------------- #
class _HangingJudgeClient:
    """Client stub whose tool call hangs forever (deadline test target)."""

    name = "hanging"

    def generate_tool_call(self, request):
        time.sleep(3600)
        raise AssertionError("should never return")


class _PromptJudgeClient:
    """Client stub returning a fixed verdict (happy-path judge)."""

    name = "prompt"

    def generate_tool_call(self, request):
        from case_engine.llm.base import ToolCallResult

        return ToolCallResult(
            tool_name="ComplianceAuditVerdict",
            arguments={"passed": False, "rationale": "bad",
                       "cited_evidence": ["x"]},
            provider="prompt",
        )


class TestG3JudgeDeadline:
    """A hung judge withholds its veto instead of stalling validation."""

    def test_deadline_produces_failopen_verdict(self, caplog, monkeypatch) -> None:
        import logging as _logging

        # configure_logging() sets propagate=False on the engine root;
        # restore it for this test so caplog can observe judge records.
        monkeypatch.setattr(_logging.getLogger("case_engine"), "propagate", True)
        caplog.set_level(_logging.WARNING, logger="case_engine")
        judge = LLMBackedJudge(_HangingJudgeClient(), timeout_ms=80)
        verdict = judge.audit("audit prompt", {"rule": "VR001", "trace": {}})
        assert verdict.passed is True  # veto withheld (fail-open)
        assert "deadline" in verdict.rationale.lower()
        assert any("withholding veto" in r.message for r in caplog.records)

    def test_no_deadline_is_direct_call(self) -> None:
        judge = LLMBackedJudge(_PromptJudgeClient(), timeout_ms=None)
        assert judge.audit("p", {"trace": {}}).passed is False

    def test_deadline_zero_disables(self) -> None:
        judge = LLMBackedJudge(_PromptJudgeClient(), timeout_ms=0)
        assert judge.audit("p", {"trace": {}}).passed is False

    def test_client_error_still_degrades_via_exception(self) -> None:
        class _Boom:
            name = "boom"

            def generate_tool_call(self, request):
                raise RuntimeError("provider down")

        judge = LLMBackedJudge(_Boom(), timeout_ms=1000)
        with pytest.raises(RuntimeError):
            judge.audit("p", {"trace": {}})


# --------------------------------------------------------------------------- #
# G4a - audit payload redaction
# --------------------------------------------------------------------------- #
class TestG4Redaction:
    """PII never reaches the ledger when the redactor is enabled."""

    def test_redactor_tokenizes_deterministically(self) -> None:
        red = PIIRedactor()
        out1, n1 = red.redact_payload({"a": "jane@corp.com", "b": ["jane@corp.com"]})
        assert n1 == 2
        assert out1["a"] == "[EMAIL_1]"
        assert out1["b"] == ["[EMAIL_1]"]
        out2, n2 = red.redact_payload({"c": "sam@corp.com"})
        assert n2 == 1 and out2["c"] == "[EMAIL_2]"

    def test_noop_redactor_is_identity(self) -> None:
        payload = {"a": "jane@corp.com"}
        out, n = NoopRedactor().redact_payload(payload)
        assert out is payload and n == 0

    def test_engine_redacts_audit_payloads(self) -> None:
        engine = _engine(
            redact_pii_in_audit=True,
            guardrails=GlobalGuardrails(pii_policy="allow_with_flags",
                                        max_reflexion_retries=1),
        )
        result = engine.run(PII_QUERY)
        assert result.all_valid is True
        rendered = json.dumps(
            [e.model_dump() for e in _events(engine.ledger)], default=str
        )
        assert "jane.doe@corp.com" not in rendered
        assert "[EMAIL_1]" in rendered

    def test_redaction_disabled_keeps_legacy_behavior(self) -> None:
        engine = _engine(
            guardrails=GlobalGuardrails(pii_policy="allow_with_flags",
                                        max_reflexion_retries=1),
        )
        engine.run(PII_QUERY)
        rendered = json.dumps(
            [e.model_dump() for e in _events(engine.ledger)], default=str
        )
        assert "jane.doe@corp.com" in rendered  # legacy, unredacted


# --------------------------------------------------------------------------- #
# G4b - payload encryption at rest
# --------------------------------------------------------------------------- #
class TestG4Encryption:
    """AESGCM envelopes: plaintext never on disk, chain stays key-free."""

    def test_key_loading(self) -> None:
        raw = os.urandom(32)
        assert load_key(raw) == raw
        b64 = base64.b64encode(raw).decode()
        assert load_key(b64) == raw
        with pytest.raises(LedgerError):
            load_key(None, key_env="CASE_ENGINE_NO_SUCH_KEY_XYZ")
        with pytest.raises(LedgerError):
            load_key(b"short")

    def test_jsonl_encrypted_roundtrip(self, tmp_path: Path) -> None:
        key = base64.b64encode(os.urandom(32)).decode()
        cipher = AEADPayloadCipher(key=key)
        path = tmp_path / "audit.jsonl"
        ledger = JSONLLedger(path, payload_cipher=cipher)
        event = ledger.append("SESSION_START", "s-1", "engine",
                              {"contact": "jane@corp.com"})
        assert event.seq == 1
        raw_text = path.read_text()
        assert "jane@corp.com" not in raw_text
        assert "__enc__" in raw_text and "AESGCM" in raw_text
        # Chain verifies WITHOUT the key (hash over ciphertext).
        assert ledger.verify_chain().valid is True
        # Raw iteration yields envelopes; decrypt=True yields plaintext.
        raw_events = list(ledger.iter_events())
        assert raw_events[0].payload.get("__enc__") == "AESGCM"
        plain_events = list(ledger.iter_events(decrypt=True))
        assert plain_events[0].payload["contact"] == "jane@corp.com"

    def test_wrong_key_fails_closed(self, tmp_path: Path) -> None:
        cipher_a = AEADPayloadCipher(key=base64.b64encode(os.urandom(32)).decode())
        path = tmp_path / "audit.jsonl"
        ledger = JSONLLedger(path, payload_cipher=cipher_a)
        ledger.append("SESSION_START", "s-1", "engine", {"v": 1})
        wrong = AEADPayloadCipher(key=base64.b64encode(os.urandom(32)).decode())
        ledger_b = JSONLLedger(path, payload_cipher=wrong)
        assert ledger_b.verify_chain().valid is True  # chain math is key-free
        with pytest.raises(LedgerError):
            list(ledger_b.iter_events(decrypt=True))

    def test_tampered_ciphertext_detected_twice(self, tmp_path: Path) -> None:
        cipher = AEADPayloadCipher(key=base64.b64encode(os.urandom(32)).decode())
        path = tmp_path / "audit.jsonl"
        ledger = JSONLLedger(path, payload_cipher=cipher)
        ledger.append("SESSION_START", "s-1", "engine", {"v": 1})
        lines = path.read_text().splitlines()
        doc = json.loads(lines[0])
        doc["payload"]["data"] = "dGFtcGVyZWQ="  # corrupt ciphertext
        path.write_text(json.dumps(doc) + "\n")
        # (1) AEAD auth fails on decrypt...
        ledger2 = JSONLLedger(path, payload_cipher=cipher)
        with pytest.raises(LedgerError):
            list(ledger2.iter_events(decrypt=True))
        # (2) ...and the hash chain no longer verifies (payload mutated).
        assert ledger2.verify_chain().valid is False

    def test_sqlite_encrypted_roundtrip(self, tmp_path: Path) -> None:
        cipher = AEADPayloadCipher(key=base64.b64encode(os.urandom(32)).decode())
        ledger = SQLiteLedger(tmp_path / "audit.sqlite3", payload_cipher=cipher)
        ledger.append("SESSION_START", "s-1", "engine", {"contact": "jane@corp.com"})
        assert ledger.verify_chain().valid is True
        raw = list(ledger.iter_events())[0].payload
        assert raw.get("__enc__") == "AESGCM"
        plain = list(ledger.iter_events(decrypt=True))[0].payload
        assert plain["contact"] == "jane@corp.com"

    def test_engine_end_to_end_encrypted_jsonl(self, tmp_path: Path,
                                               monkeypatch) -> None:
        monkeypatch.setenv(
            "CASE_ENGINE_LEDGER_KEY",
            base64.b64encode(os.urandom(32)).decode(),
        )
        ledger_path = tmp_path / "case_audit.jsonl"
        engine = CASEEngine(
            config=EngineConfig(
                ledger_backend="jsonl",
                ledger_path=str(ledger_path),
                ledger_encryption=True,
                guardrails=GlobalGuardrails(max_reflexion_retries=1),
            ),
            llm_client=MockLLMClient.from_registry_examples(
                __import__("case_engine.registry", fromlist=["CognitiveRegistry"])
                .CognitiveRegistry.load_default()
            ),
        )
        result = engine.run(CLEAN_QUERY)
        assert result.all_valid is True
        raw_text = ledger_path.read_text()
        assert CLEAN_QUERY not in raw_text  # payloads are ciphertext
        assert "__enc__" in raw_text
        assert engine.verify_audit_chain().valid is True

    def test_encryption_requires_key(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CASE_ENGINE_LEDGER_KEY", raising=False)
        with pytest.raises(Exception):
            CASEEngine(
                config=EngineConfig(
                    ledger_backend="jsonl",
                    ledger_path=str(tmp_path / "x.jsonl"),
                    ledger_encryption=True,
                ),
                llm_client=MockLLMClient(),
            )

    def test_create_ledger_factory_passes_cipher(self, tmp_path: Path) -> None:
        cipher = AEADPayloadCipher(key=base64.b64encode(os.urandom(32)).decode())
        ledger = create_ledger("jsonl", str(tmp_path / "a.jsonl"),
                               payload_cipher=cipher)
        ledger.append("SESSION_START", "s-1", "engine", {"v": 1})
        assert "__enc__" in (tmp_path / "a.jsonl").read_text()


# --------------------------------------------------------------------------- #
# Regression - the topology table carries the new edges
# --------------------------------------------------------------------------- #
class TestNewEdges:
    """The G1/G2 edges are declared and reachable."""

    def test_node1_pii_escalation_edge(self) -> None:
        from case_engine.pipeline.state_machine import EDGES

        assert EDGES["node_1_intent_classifier"]["pii_escalation"] == \
            "node_8_human_escalation"

    def test_node6_passthrough_edge(self) -> None:
        from case_engine.pipeline.state_machine import EDGES

        assert EDGES["node_6_reflexion_loop"]["passthrough"] == \
            "node_7_audit_and_synthesis"

    def test_genesis_unchanged(self) -> None:
        assert GENESIS_HASH  # chain anchor untouched by G4
