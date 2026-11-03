"""G28 — mutation-hardening pins for the rule logic.

Every test in this module exists because the mutation baseline
(``docs/MUTATION_BASELINE.md``) showed a surviving mutant: a boundary
or conjunction whose flip was not observable through the example-based
rule suite.  Each test pins the *observable contract* that kills the
mutant — they are behaviour pins, not implementation tests:

========================  ==============================================
surviving mutant          killing pin
========================  ==============================================
``_short`` ``<=``→``<``   exact-limit strings returned verbatim
``_shared_content``       plural-tolerance never invents shared tokens
VR001 empty observe       empty observations are a rejection
VR004 ``>=``              1:1 Pareto splits still fail
VR005 systemic keywords   non-systemic causes fail closed
VR006 conjunctions        empty answer / malformed entries / match paths
VR007 ``True`` default    un-annotated high budgets pass (default True)
VR013 unknown domain      unknown domains are a rejection
VR016 ``<`` boundary      confidence == threshold passes
run_rule guard            misconfigured VALIDATORS raise RegistryError
judge activation          non-semantic rules never call the judge
judge veto message        passing traces never carry veto wording
========================  ==============================================

The remaining documented survivors in the baseline are
equivalent-at-verdict mutants (e.g. ``include_url=False`` in the gate's
error flattening, where the URL key is discarded) and are justified in
that document.

Cross-references: ``tools/mutation_run.py``, ``docs/GAP_ANALYSIS.md``
§G28, ``tests/unit/test_validation_rules.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from case_engine.config import GlobalGuardrails
from case_engine.exceptions import RegistryError
from case_engine.registry import CognitiveRegistry
from case_engine.validation.rules import (
    _shared_content,
    _short,
    content_tokens,
    run_rule,
)

GUARDRAILS = GlobalGuardrails()


def _trace(**fields) -> SimpleNamespace:
    """Build a duck-typed trace (same convention as the rule suite)."""
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------- #
# Helper boundary pins
# --------------------------------------------------------------------- #


def test_short_returns_exact_limit_string_verbatim() -> None:
    """Kills ``_short`` ``<=``→``<``: len == limit is NOT truncated."""
    text = "x" * 12
    assert _short(text, 12) == text


def test_shared_content_plural_tolerance_is_symmetric_and_exact() -> None:
    """Kills the ``and``→``or`` in the plural-tolerance union.

    Tokens ending in ``s`` whose singular is absent from the other side
    must NOT count as shared; flipping the conjunction would admit them
    and inflate VR005/VR014 token overlap.
    """
    # "things"/"boxes" have no singular counterpart on the other side.
    assert _shared_content("things boxes", "cat dog") == set()
    # Mirror side: tokens on the SECOND argument ending in "s" whose
    # singular is absent from the first must not count either (kills the
    # symmetric ``and``→``or`` mutant).
    assert _shared_content("cat", "things") == set()
    # Singular/plural pairs still count (the tolerance itself).
    assert _shared_content("cat", "cats") == {"cats"}
    # Single-character tokens never participate at all.
    assert content_tokens("a b c") == set()


# --------------------------------------------------------------------- #
# Rule verdict pins
# --------------------------------------------------------------------- #


def test_vr001_empty_observations_fail() -> None:
    """Kills ``return False``→``True`` on the empty-observations branch."""
    report = run_rule(
        "VR001", _trace(observe_raw_data=[], orient_context="anything"), None, GUARDRAILS,
    )
    assert report.passed is False
    assert "observe_raw_data is empty" in report.message


def test_vr004_equal_pareto_counts_fail() -> None:
    """Kills VR004 ``>=``→``>``: a 1:1 split is still a failure."""
    trace = _trace(vital_few_20_percent=["a", "b"], trivial_many_80_percent=["x", "y"])
    report = run_rule("VR004", trace, None, GUARDRAILS)
    assert report.passed is False
    assert "strictly smaller" in report.message


def test_vr005_non_systemic_root_cause_fails() -> None:
    """Kills the ``return False`` flip on the no-systemic-keyword branch."""
    trace = _trace(validated_root_cause="Bob was tired that day")
    report = run_rule("VR005", trace, None, GUARDRAILS)
    assert report.passed is False
    assert "systemic" in report.message


def test_vr006_empty_answer_with_hypotheses_fails() -> None:
    """Kills ``not hyps or not answer``→``and`` and its ``False``→``True``.

    With hypotheses present but an empty answer the rule must reject;
    under the flip the match loop proceeds and ``"" in low`` makes the
    wrong hypothesis "match".
    """
    trace = _trace(
        hypotheses=[
            {"explanation": "gateway timeouts", "unverified_assumptions_count": 1},
        ],
        simplest_viable_answer="",
    )
    report = run_rule("VR006", trace, None, GUARDRAILS)
    assert report.passed is False
    assert "missing" in report.message


def test_vr006_malformed_hypothesis_entry_fails() -> None:
    """Kills the malformed-entry ``return False`` flip (missing count)."""
    trace = _trace(
        hypotheses=[{"explanation": "no count here"}],
        simplest_viable_answer="no count here",
    )
    report = run_rule("VR006", trace, None, GUARDRAILS)
    assert report.passed is False
    assert "malformed" in report.message


@pytest.mark.parametrize(
    ("explanation", "answer"),
    [
        # Containment via a single-token explanation (kills the first
        # ``or``→``and``: only the containment disjunct is True).
        ("gateway", "gateway hiccup"),
        # Answer-contains-explanation with shared < 2 (kills the second
        # ``or``→``and``).
        ("gateway timeout errors here", "errors"),
        # Exactly 2 shared content tokens, no containment (kills the
        # ``>=``→``>`` on the shared-token threshold).
        ("payment gateway failed", "gateway failed again"),
    ],
)
def test_vr006_match_paths_pass(explanation: str, answer: str) -> None:
    """Each documented match path (containment / reverse / 2-token) passes."""
    trace = _trace(
        hypotheses=[
            {"explanation": "expensive narrative", "unverified_assumptions_count": 5},
            {"explanation": explanation, "unverified_assumptions_count": 1},
        ],
        simplest_viable_answer=answer,
    )
    report = run_rule("VR006", trace, None, GUARDRAILS)
    assert report.passed is True, report.message


def test_vr007_missing_matters_attribute_defaults_to_true() -> None:
    """Kills the ``getattr(..., True)``→``False`` default flip.

    A trace with a large emotional budget but NO ``matters_in_five_years``
    annotation must pass (long-term significance is the default), which
    is only observable when the budget exceeds the 5-minute ceiling.
    """
    trace = _trace(emotional_budget_allocated_minutes=30)
    report = run_rule("VR007", trace, None, GUARDRAILS)
    assert report.passed is True, report.message


def test_vr013_unknown_domain_fails() -> None:
    """Kills the unknown-domain ``return False`` flip in VR013."""
    trace = _trace(assigned_domain="Bizarro", selected_methodology="anything")
    report = run_rule("VR013", trace, None, GUARDRAILS)
    assert report.passed is False
    assert "unknown assigned_domain" in report.message


def test_vr016_confidence_exactly_at_threshold_passes() -> None:
    """Kills VR016 ``<``→``<=``: meeting the threshold is a pass."""
    threshold = GUARDRAILS.default_confidence_threshold
    trace = _trace(confidence=threshold)
    report = run_rule("VR016", trace, None, GUARDRAILS)
    assert report.passed is True, report.message


# --------------------------------------------------------------------- #
# run_rule plumbing pins
# --------------------------------------------------------------------- #


def test_run_rule_raises_registry_error_when_validator_missing(monkeypatch) -> None:
    """Kills the ``or``→``and`` in the implementation guard.

    A rule present in the index but missing from ``VALIDATORS`` is a
    deploy-time misconfiguration and must raise ``RegistryError`` — not
    fall into the fail-closed runtime catch.
    """
    from case_engine.validation import rules as rules_module

    patched = {k: v for k, v in rules_module.VALIDATORS.items() if k != "VR001"}
    monkeypatch.setattr(rules_module, "VALIDATORS", patched)
    trace = _trace(
        observe_raw_data=["Sensor A 95% moisture", "New irrigation motor installed"],
        orient_context="The spike correlates with the motor installation, likely EMI",
    )
    with pytest.raises(RegistryError):
        run_rule("VR001", trace, None, GUARDRAILS)


def test_semantic_rule_judge_veto_message_only_on_failed_heuristic() -> None:
    """Kills the veto-message ``and``→``or``.

    When the heuristic PASSES and the judge agrees, the message must
    stay clean — under the flip a passing trace would carry the
    contradictory "judge agreed to pass but heuristic failed" wording.
    """
    calls: list[tuple] = []

    def _audit(prompt, payload):
        calls.append((prompt, payload))
        return SimpleNamespace(passed=True, rationale="looks right")

    judge = SimpleNamespace(enabled=True, audit=_audit)
    trace = _trace(
        observe_raw_data=["Sensor A 95% moisture", "New irrigation motor installed"],
        orient_context="The spike correlates with the motor installation, likely EMI",
    )
    report = run_rule("VR001", trace, None, GUARDRAILS, judge=judge)
    assert report.passed is True
    assert "judge agreed" not in report.message
    assert len(calls) == 1  # the judge WAS consulted for the semantic rule


def test_judge_without_enabled_attribute_is_never_consulted() -> None:
    """Kills the ``getattr(judge, 'enabled', False)`` default flip.

    A judge object without an ``enabled`` attribute is disabled by
    contract: no audit call may happen (the flip would call it).
    """
    calls: list[tuple] = []

    def _audit(prompt, payload):
        calls.append((prompt, payload))
        return SimpleNamespace(passed=False, rationale="veto everything")

    judge = SimpleNamespace(audit=_audit)  # NOTE: no `enabled` attribute
    trace = _trace(observe_raw_data=[], orient_context="anything")
    report = run_rule("VR001", trace, None, GUARDRAILS, judge=judge)
    assert report.passed is False
    assert calls == [], "disabled judge was consulted"
    assert "judge" not in report.message


def test_non_semantic_rule_never_calls_the_judge() -> None:
    """Kills the ``rule_id in SEMANTIC_RULE_IDS`` conjunction flip.

    Only VR001/VR002/VR005/VR012/VR014 are judge-audited by design; a
    deterministic structural rule (VR004) must not reach the judge even
    when an enabled judge is wired.
    """
    calls: list[tuple] = []

    def _audit(prompt, payload):
        calls.append((prompt, payload))
        return SimpleNamespace(passed=True, rationale="ok")

    judge = SimpleNamespace(enabled=True, audit=_audit)
    trace = _trace(vital_few_20_percent=["a"], trivial_many_80_percent=["x", "y"])
    report = run_rule("VR004", trace, None, GUARDRAILS, judge=judge)
    assert report.passed is True
    assert calls == [], "non-semantic rule reached the judge"


def test_judge_receives_registry_auditor_prompt() -> None:
    """Kills the ``auditor_prompt or fallback`` ``or``→``and`` flip.

    The registry's curated auditor prompt must be forwarded verbatim;
    under the flip the generic fallback string is sent instead.
    """
    registry = CognitiveRegistry.load_default()
    captured: list[tuple] = []

    def _audit(prompt, payload):
        captured.append((prompt, payload))
        return SimpleNamespace(passed=True, rationale="ok")

    judge = SimpleNamespace(enabled=True, audit=_audit)
    trace = _trace(
        observe_raw_data=["Sensor A 95% moisture", "New irrigation motor installed"],
        orient_context="The spike correlates with the motor installation, likely EMI",
    )
    run_rule("VR001", trace, None, GUARDRAILS, judge=judge)
    assert captured, "judge was not consulted"
    expected_prompt = registry.get_rule("VR001").auditor_prompt
    assert captured[0][0] == expected_prompt
