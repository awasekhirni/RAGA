"""Unit tests: one passing + one failing case for EVERY rule VR001–VR016.

The rules are pure functions over duck-typed traces, so the suite drives
them with ``SimpleNamespace`` fixtures (no schema dependency) and the
real ``run_rule`` wrapper — verifying registry metadata wiring, judge
combination and fail-closed behaviour too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from case_engine.config import GlobalGuardrails
from case_engine.exceptions import RegistryError
from case_engine.llm import MockLLMClient
from case_engine.registry import CognitiveRegistry
from case_engine.validation import LLMBackedJudge
from case_engine.validation.rules import run_rule

GUARDRAILS = GlobalGuardrails()


def _trace(**fields) -> SimpleNamespace:
    """Build a duck-typed trace."""
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------- #
# Gold/failing traces per rule
# --------------------------------------------------------------------------- #
def vr001_valid():
    return _trace(
        observe_raw_data=["Sensor A 95% moisture", "New irrigation motor installed"],
        orient_context="The spike correlates with the motor installation, likely EMI",
    )


def vr001_invalid():
    return _trace(
        observe_raw_data=["Sensor A 95% moisture", "New irrigation motor installed"],
        orient_context="The weather is lovely and the team had lunch",
    )


def vr002_valid():
    return _trace(fundamental_truths=["Data needs 1KB/hour", "Farm has Wi-Fi coverage",
                                      "Solar power available"])


def vr002_invalid():
    return _trace(fundamental_truths=["This is similar to a typical SaaS stack"])


def vr003_valid():
    return _trace(black_hat_risks=["ramp-up", "debug complexity"],
                  green_hat_creativity=["hybrid", "gitops"])


def vr003_invalid():
    return _trace(black_hat_risks=["a", "b", "c"], green_hat_creativity=["one"])


def vr004_valid():
    return _trace(vital_few_20_percent=["q1", "q2"],
                  trivial_many_80_percent=["q3", "q4", "q5", "q6"])


def vr004_invalid():
    return _trace(vital_few_20_percent=["q1", "q2", "q3"],
                  trivial_many_80_percent=["q4", "q5"])


def vr005_valid():
    return _trace(validated_root_cause="Missing queue-depth alerting allowed silent consumer lag")


def vr005_invalid():
    return _trace(validated_root_cause="The engineer forgot to configure the alert")


def vr006_valid():
    return _trace(
        hypotheses=[
            {"explanation": "DB connection pool exhausted", "unverified_assumptions_count": 3},
            {"explanation": "Null pointer in user serializer", "unverified_assumptions_count": 1},
        ],
        simplest_viable_answer="Null pointer in user serializer",
    )


def vr006_invalid():
    return _trace(
        hypotheses=[
            {"explanation": "DB connection pool exhausted", "unverified_assumptions_count": 3},
            {"explanation": "Null pointer in user serializer", "unverified_assumptions_count": 1},
        ],
        simplest_viable_answer="DB connection pool exhausted",
    )


def vr007_valid():
    return _trace(matters_in_five_years=False, emotional_budget_allocated_minutes=5)


def vr007_invalid():
    return _trace(matters_in_five_years=False, emotional_budget_allocated_minutes=8)


def vr008_valid():
    return _trace(simple_v1_working_system="Single Node.js service + PostgreSQL + 10 sensors")


def vr008_invalid():
    return _trace(simple_v1_working_system="API server, database, cache, queue, workers")


def vr009_valid():
    return _trace(critical_failure_vectors=["a", "b", "c", "d"],
                  preemptive_safeguards=["1", "2", "3", "4"])


def vr009_invalid():
    return _trace(critical_failure_vectors=["a", "b", "c"], preemptive_safeguards=["1"])


def vr010_valid():
    return _trace(characterization_tests_required=["test_a", "test_b"])


def vr010_invalid():
    return _trace(characterization_tests_required=[])


def vr011_valid():
    return _trace(first_order_consequences=["x", "y"],
                  second_order_consequences=["x", "y", "z"],
                  third_order_consequences=["x", "y", "z"])


def vr011_invalid():
    return _trace(first_order_consequences=["only one"],
                  second_order_consequences=["x", "y"],
                  third_order_consequences=["x", "y"])


def vr012_valid():
    return _trace(proposed_team_topology="3 stream-aligned teams each owning services end-to-end")


def vr012_invalid():
    return _trace(proposed_team_topology="One big team where everyone helps each other")


def vr013_valid():
    return _trace(assigned_domain="Complex", selected_methodology="Agile experiments + cohort probing")


def vr013_invalid():
    return _trace(assigned_domain="Complex", selected_methodology="Strict waterfall plan")


def vr014_valid():
    return _trace(anticipated_gaming_vector="Engineers split work into tiny PRs to inflate count",
                  mandatory_counter_metric="Average PR size + post-merge bug rate")


def vr014_invalid():
    return _trace(anticipated_gaming_vector="Engineers split work into tiny PRs to inflate count",
                  mandatory_counter_metric="Coffee quality score in the kitchen")


def vr015_valid():
    return _trace(expected_drift_vectors=["battery", "interference", "damage"],
                  observability_requirements=["voltage metric", "rssi", "checksum rate"])


def vr015_invalid():
    return _trace(expected_drift_vectors=["battery", "interference", "damage"],
                  observability_requirements=["voltage metric"])


def vr016_valid(registry):
    return _trace(confidence=0.9), registry.get("MM001")  # threshold 0.7


def vr016_invalid(registry):
    return _trace(confidence=0.5), registry.get("MM002")  # threshold 0.8


CASES = {
    "VR001": (vr001_valid, vr001_invalid),
    "VR002": (vr002_valid, vr002_invalid),
    "VR003": (vr003_valid, vr003_invalid),
    "VR004": (vr004_valid, vr004_invalid),
    "VR005": (vr005_valid, vr005_invalid),
    "VR006": (vr006_valid, vr006_invalid),
    "VR007": (vr007_valid, vr007_invalid),
    "VR008": (vr008_valid, vr008_invalid),
    "VR009": (vr009_valid, vr009_invalid),
    "VR010": (vr010_valid, vr010_invalid),
    "VR011": (vr011_valid, vr011_invalid),
    "VR012": (vr012_valid, vr012_invalid),
    "VR013": (vr013_valid, vr013_invalid),
    "VR014": (vr014_valid, vr014_invalid),
    "VR015": (vr015_valid, vr015_invalid),
}


@pytest.mark.parametrize("rule_id", sorted(CASES))
class TestDeterministicRules:
    """Every rule: gold trace passes, poisoned trace fails, metadata wired."""

    def test_valid_case_passes(self, rule_id, registry) -> None:
        factory, _ = CASES[rule_id]
        report = run_rule(rule_id, factory(), registry.get(_model_for(rule_id)), GUARDRAILS)
        assert report.passed, report.message
        assert report.rule_id == rule_id
        assert report.rule_name == _rule_name(registry, rule_id)
        assert not report.judge_used

    def test_invalid_case_fails(self, rule_id, registry) -> None:
        _, broken = CASES[rule_id]
        report = run_rule(rule_id, broken(), registry.get(_model_for(rule_id)), GUARDRAILS)
        assert not report.passed, f"{rule_id} should reject: {report.message}"


class TestVR016Universal:
    """VR016 needs the owning model's threshold."""

    def test_passes_with_model_threshold(self, registry) -> None:
        trace, model = vr016_valid(registry)
        assert run_rule("VR016", trace, model, GUARDRAILS).passed

    def test_fails_below_threshold(self, registry) -> None:
        trace, model = vr016_invalid(registry)
        assert not run_rule("VR016", trace, model, GUARDRAILS).passed

    def test_falls_back_to_default_threshold(self) -> None:
        assert run_rule("VR016", _trace(confidence=0.75), None, GUARDRAILS).passed
        assert not run_rule("VR016", _trace(confidence=0.4), None, GUARDRAILS).passed


class TestJudgeUpgrade:
    """LLM-judge veto semantics for semantic rules."""

    def test_judge_veto_fails_a_passing_heuristic(self, registry) -> None:
        judge = LLMBackedJudge(MockLLMClient(script={
            "ComplianceAuditVerdict": {"passed": False, "rationale": "weak",
                                       "cited_evidence": ["x"]}}))
        report = run_rule("VR014", vr014_valid(), registry.get("MM014"),
                          GUARDRAILS, judge=judge)
        assert not report.passed and report.judge_used

    def test_judge_cannot_rescue_a_failing_heuristic(self, registry) -> None:
        judge = LLMBackedJudge(MockLLMClient(script={
            "ComplianceAuditVerdict": {"passed": True, "rationale": "fine",
                                       "cited_evidence": ["x"]}}))
        report = run_rule("VR014", vr014_invalid(), registry.get("MM014"),
                          GUARDRAILS, judge=judge)
        assert not report.passed and report.judge_used

    def test_judge_failure_downgrades_to_heuristic(self, registry) -> None:
        judge = LLMBackedJudge(MockLLMClient(script={}))  # no audit script -> error
        report = run_rule("VR014", vr014_valid(), registry.get("MM014"),
                          GUARDRAILS, judge=judge)
        assert report.passed and not report.judge_used


class TestFailClosed:
    """A crashing rule must fail the trace, never crash the pipeline."""

    def test_crashing_rule_fails_closed(self, registry) -> None:
        exploding = _trace(confidence="not-a-number")  # float() raises ValueError
        report = run_rule("VR016", exploding, None, GUARDRAILS)
        assert not report.passed
        assert "fail-closed" in report.message or "errored" in report.message

    def test_unknown_rule_id_raises(self, registry) -> None:
        with pytest.raises(RegistryError):
            run_rule("VR999", _trace(), None, GUARDRAILS)


def _rule_name(registry: CognitiveRegistry, rule_id: str) -> str:
    return registry.get_rule(rule_id).name


def _model_for(rule_id: str) -> str:
    return {
        "VR001": "MM001", "VR002": "MM002", "VR003": "MM003", "VR004": "MM004",
        "VR005": "MM005", "VR006": "MM006", "VR007": "MM007", "VR008": "MM008",
        "VR009": "MM009", "VR010": "MM010", "VR011": "MM011", "VR012": "MM012",
        "VR013": "MM013", "VR014": "MM014", "VR015": "MM015",
    }[rule_id]


# --------------------------------------------------------------------------- #
# G28 mutation-hardening: boundary pins for the rule helpers.
# These exist because mutation testing showed the helper boundaries were
# unpinned (a `<=` -> `<` flip in `_short` and an `and` -> `or` flip in
# `content_tokens` survived the rule suite).  They are behaviour pins,
# not implementation tests: each asserts an observable contract.
# --------------------------------------------------------------------------- #
from case_engine.validation.rules import _short


class TestHelperBoundaryPins:
    """Kill the boundary mutants of the rule helpers (G28 baseline)."""

    @pytest.mark.parametrize("limit", [5, 12, 80])
    def test_short_returns_input_unchanged_at_exact_limit(self, limit: int) -> None:
        """A string of exactly ``limit`` chars is returned verbatim."""
        text = "x" * limit
        assert _short(text, limit) == text

    def test_short_truncates_above_limit_with_ellipsis(self) -> None:
        """Above the limit the output is bounded and ends with an ellipsis."""
        text = "y" * 41
        out = _short(text, 40)
        assert len(out) <= 40
        assert out.endswith("…")

    def test_content_tokens_exclude_short_and_stopword_tokens(self) -> None:
        """Single-char tokens are dropped even when not stopwords.

        Pins the ``not in STOPWORDS and len >= 2`` conjunction: flipping
        the ``and`` to ``or`` would admit single-char or stopword-only
        token sets and break token-overlap rules (VR005/VR014 family).
        """
        from case_engine.validation.rules import content_tokens

        assert content_tokens("a b c") == set()
        assert content_tokens("ab") == {"ab"}
        # Stopword-only input yields nothing.
        assert content_tokens("the and of") == set()
