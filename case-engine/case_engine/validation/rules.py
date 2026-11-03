"""The 16 deterministic validation-rule implementations (VR001–VR016).

Security role
-------------
This module is the *content* half of the engine's zero-trust stack:
``case_engine.enforcement.gate`` proves a payload is well-formed
(schema); the rules here decide whether the reasoning inside it is
well-reasoned (mental-model contract).  Every rule is a deterministic,
fully offline proxy for the spec's judgement criteria; the five semantic
rules can additionally be vetoed by an optional LLM-as-judge, but the
deterministic layer always runs first and can never be bypassed.

Design contract
---------------
* Every rule is a **pure function** of ``(trace, model_def, guardrails)``
  returning ``(passed: bool, message: str)``.  No I/O, no clock, no
  randomness → fully unit-testable and replayable from the audit ledger.
* Rules operate duck-typed on trace objects (Pydantic models in
  production; ``SimpleNamespace`` in unit tests).
* Semantic rules (VR001/002/005/012/014) additionally support the
  optional LLM-as-judge veto; see :mod:`case_engine.validation.llm_judge`.
* ``run_rule`` never lets a rule crash the pipeline: any unexpected
  exception fails the rule (fail-closed) and is logged with a traceback.

Logging contract
----------------
Each rule logs a DEBUG entry (rule id + truncated inputs) and, when it
rejects a trace, a WARNING carrying the rejection reason.  Successful
evaluations are logged once by ``run_rule`` ("Rule executed", DEBUG)
instead of twice.  Trace *content* is never logged untruncated: strings
are squeezed through :func:`_short` so structured log lines stay
bounded even on adversarially large payloads.  The private text helpers
(``_tokens``, ``_shared_content``) are a deliberately log-free hot path
— their callers provide the traceability.

Contents
--------
- ``_vr001`` … ``_vr016`` -- the 16 rule implementations.
- ``VALIDATORS`` -- rule-id dispatch table.
- ``run_rule`` -- fail-closed wrapper with judge upgrade.
- ``content_tokens`` / ``_tokens`` / ``_shared_content`` / ``_short`` --
  text and logging helpers shared by the heuristic rules.

Cross-references
----------------
docs/VALIDATION_RULES.md (per-rule semantics and the spec's judge
prompts), docs/ARCHITECTURE.md §1.3 (fail-closed rationale).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from case_engine.config import GlobalGuardrails
from case_engine.exceptions import RegistryError
from case_engine.logging_setup import get_logger
from case_engine.messages import msg
from case_engine.registry.models import MentalModelDefinition
from case_engine.registry.rule_data import CYNEFIN_METHODOLOGY_KEYWORDS, RULES
from case_engine.schemas.reports import RuleReport
from case_engine.validation.llm_judge import SEMANTIC_RULE_IDS, LLMJudge

__all__ = ["run_rule", "VALIDATORS", "content_tokens"]

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Text helpers shared by heuristic rules
# --------------------------------------------------------------------------- #
_STOPWORDS: frozenset[str] = frozenset(
    """the a an and or of to in on for with is are was were be been by at as it
    its this that these those from into not no but if then so we they their
    there when while will would can could should may might must do does did
    have has had per via up out about over under after before new use using
    used very also just than too own same such only more most some any each""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Maximum characters of any value emitted into structured log extras.
_LOG_LIMIT = 160


def _short(text: Any, limit: int = _LOG_LIMIT) -> str:
    """Truncate *text* for inclusion in structured log extras.

    Log lines must stay bounded even when a trace field is adversarially
    large, and trace content must never reach log levels above DEBUG in
    untruncated form.  This helper is the single truncation point for
    rule logging (the rejection *messages* returned to callers are
    never truncated here — only their logged copies are).

    Args:
        text: Any value (coerced with ``str``).
        limit: Maximum characters kept; an ellipsis marks the cut.

    Returns:
        The truncated string.
    """
    rendered = str(text)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _tokens(text: str) -> set[str]:
    """Tokenize *text* into a normalized term set.

    Normalization: lowercase alphanumeric runs.  Stemming is deliberately
    NOT applied here; plural tolerance is handled at comparison time by
    :func:`_shared_content` so both sides stay symmetric.

    Args:
        text: Arbitrary text.

    Returns:
        Set of normalized terms.
    """
    return set(_TOKEN_RE.findall((text or "").lower()))


def content_tokens(text: str) -> set[str]:
    """Return content-bearing tokens (stopwords and 1-char terms removed).

    Used by every semantic-overlap heuristic (VR001, VR006, VR014) so
    that "the sensor data" and "sensor" can match while filler words
    cannot manufacture agreement.

    Args:
        text: Arbitrary text.

    Returns:
        Filtered token set used by semantic-overlap heuristics.

    Example:
        >>> sorted(content_tokens("The Sensors reported 3 alerts"))
        ['alerts', 'reported', 'sensors']
    """
    return {t for t in _tokens(text) if t not in _STOPWORDS and len(t) >= 2}


def _shared_content(text_a: str, text_b: str) -> set[str]:
    """Return the shared content tokens between two texts (plural-tolerant).

    A term matches when the forms are equal OR one is the other plus a
    trailing ``s`` (``PR`` ~ ``PRs``, ``sensor`` ~ ``sensors``).

    Args:
        text_a: First text.
        text_b: Second text.

    Returns:
        Set of shared terms (in *text_a*'s form).
    """
    ta, tb = content_tokens(text_a), content_tokens(text_b)
    shared = ta & tb
    shared |= {t for t in ta if t.endswith("s") and t[:-1] in tb}
    shared |= {t for t in tb if t.endswith("s") and t[:-1] in ta}
    return shared


# --------------------------------------------------------------------------- #
# VR001 — OODA: orient must reference observe data
# --------------------------------------------------------------------------- #
def _vr001(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR001: ``orient_context`` must reference >=1 ``observe_raw_data`` item.

    Checks: the OODA loop's Orient phase may only interpret data that
    was actually observed — an ``orient_context`` introducing facts
    absent from ``observe_raw_data`` is hallucinating its premises.

    Heuristic: an observe item counts as referenced when it shares at
    least one content token with ``orient_context`` (deterministic proxy
    for the spec's "embedding similarity > 0.6" hint; upgradeable via the
    LLM judge, which can veto but never rescue).

    Inputs expected: ``trace.observe_raw_data`` (non-empty list of
    strings), ``trace.orient_context`` (string).

    Passes when: >=1 observe item shares a content token with the orient
    text.  Fails when: the item list is empty (nothing was observed), or
    no item shares a token.

    Protects: MM001 (OODA Loop) observe→orient grounding contract.

    Fail example: observe ``["Sensor A 95% moisture"]`` with orient
    ``"The weather is lovely"`` — no shared token → rejected.

    Args:
        trace: MM001 trace (duck-typed).
        model_def: Registry definition (unused, kept for uniform signature).
        guardrails: Global guardrails (unused).

    Returns:
        ``(passed, message)``.
    """
    orient = getattr(trace, "orient_context", "") or ""
    items = list(getattr(trace, "observe_raw_data", None) or [])
    log.debug(
        "VR001 evaluating orient->observe linkage",
        extra={"rule_id": "VR001", "n_observe_items": len(items),
               "orient_context": _short(orient, 80)},
    )
    if not items:
        message = "VR001 failed: observe_raw_data is empty; nothing was observed."
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR001", "reason": _short(message)})
        return False, message
    matched = [item for item in items if _shared_content(item, orient)]
    if matched:
        preview = "; ".join(m[:60] for m in matched[:3])
        return True, (
            f"VR001 passed: orient_context references {len(matched)}/{len(items)} "
            f"observe items (e.g. {preview!r})."
        )
    message = (
        "VR001 failed: orient_context introduces entirely new facts and "
        "references none of the observe_raw_data items."
    )
    log.warning("Validation rule rejected trace",
                extra={"rule_id": "VR001", "reason": _short(message)})
    return False, message


# --------------------------------------------------------------------------- #
# VR002 — First Principles: no analogy markers in fundamental_truths
# --------------------------------------------------------------------------- #
_ANALOGY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"similar to", r"\blike (a|an|the)\b", r"just like", r"\bas done by\b",
        r"\bas used by\b", r"industry standard", r"\bakin to\b", r"\bsame as\b",
        r"reminds me of", r"\btypical of\b", r"\bbasically a\b",
    )
)


def _vr002(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR002: ``fundamental_truths`` must be analogy-free.

    Checks: each entry of ``fundamental_truths`` is scanned
    (case-insensitively) for analogy markers — ``similar to``,
    ``like a/an/the``, ``just like``, ``as done by``, ``as used by``,
    ``industry standard``, ``akin to``, ``same as``, ``reminds me of``,
    ``typical of``, ``basically a``.

    Inputs expected: ``trace.fundamental_truths`` (list of strings).

    Passes when: no truth contains any marker.  Fails when: any marker
    matches; the message quotes the offending truth (first 60 chars)
    and the matched marker, at most 5 offenders.

    Protects: MM002 (First Principles Reasoning) — truths must come from
    decomposing to fundamentals, not from copying another domain's
    solution ("we'll just build it like Uber").

    Fail example: ``["This is similar to a typical SaaS stack"]`` →
    rejected on ``similar to`` and ``typical of``.

    Args:
        trace: MM002 trace (duck-typed).
        model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` listing any offending markers.
    """
    truths = list(getattr(trace, "fundamental_truths", None) or [])
    log.debug(
        "VR002 scanning truths for analogy markers",
        extra={"rule_id": "VR002", "n_truths": len(truths)},
    )
    offenders: list[str] = []
    for truth in truths:
        for pattern in _ANALOGY_PATTERNS:
            match = pattern.search(truth)
            if match:
                offenders.append(f"{truth[:60]!r} -> {match.group(0)!r}")
    if offenders:
        message = (
            "VR002 failed: fundamental_truths contain analogy markers: "
            + "; ".join(offenders[:5])
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR002", "reason": _short(message)})
        return False, message
    return True, f"VR002 passed: {len(truths)} truths contain no analogy markers."


# --------------------------------------------------------------------------- #
# VR003 — Six Hats: black hat must not dominate green hat
# --------------------------------------------------------------------------- #
def _vr003(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR003: ``len(black_hat_risks) <= len(green_hat_creativity)``.

    Checks: Six Hats balance — the critical Black Hat list must not be
    longer than the generative Green Hat list, otherwise caution
    dominates creativity and the ideation phase degenerates into pure
    risk listing.

    Inputs expected: ``trace.black_hat_risks`` and
    ``trace.green_hat_creativity`` (lists; missing attributes count as
    empty).

    Passes when: ``risks <= ideas`` (including both empty).  Fails when:
    ``risks > ideas``; the message quotes both counts.

    Protects: MM003 (Six Thinking Hats) ideation-balance contract.

    Fail example: 3 risks vs 1 creative alternative → rejected.

    Args:
        trace: MM003 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with both counts.
    """
    risks = list(getattr(trace, "black_hat_risks", None) or [])
    ideas = list(getattr(trace, "green_hat_creativity", None) or [])
    log.debug(
        "VR003 comparing hat counts",
        extra={"rule_id": "VR003", "n_risks": len(risks), "n_ideas": len(ideas)},
    )
    if len(risks) > len(ideas):
        message = (
            f"VR003 failed: {len(risks)} risks vs {len(ideas)} creative "
            "alternatives - caution is dominating creativity."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR003", "reason": _short(message)})
        return False, message
    return True, f"VR003 passed: {len(risks)} risks <= {len(ideas)} creative alternatives."


# --------------------------------------------------------------------------- #
# VR004 — Pareto: vital few strictly smaller than trivial many
# --------------------------------------------------------------------------- #
def _vr004(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR004: ``len(vital_few_20_percent) < len(trivial_many_80_percent)``.

    Checks: the Pareto decomposition must be strictly ordered — the
    "vital few" must be a strict minority of the "trivial many",
    otherwise the 80/20 split is decorative rather than analytical.

    Inputs expected: ``trace.vital_few_20_percent`` and
    ``trace.trivial_many_80_percent`` (lists).

    Passes when: ``vital < trivial`` (strictly).  Fails when: equal or
    larger — note the strict inequality makes even a 1:1 split a
    failure.

    Protects: MM004 (Pareto Principle) prioritisation contract.

    Fail example: vital=3, trivial=2 → rejected.

    Args:
        trace: MM004 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with both counts.
    """
    vital = list(getattr(trace, "vital_few_20_percent", None) or [])
    trivial = list(getattr(trace, "trivial_many_80_percent", None) or [])
    log.debug(
        "VR004 comparing Pareto counts",
        extra={"rule_id": "VR004", "n_vital": len(vital), "n_trivial": len(trivial)},
    )
    if len(vital) >= len(trivial):
        message = (
            f"VR004 failed: vital_few ({len(vital)}) must be strictly smaller "
            f"than trivial_many ({len(trivial)})."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR004", "reason": _short(message)})
        return False, message
    return True, f"VR004 passed: {len(vital)} vital < {len(trivial)} trivial."


# --------------------------------------------------------------------------- #
# VR005 — 5 Whys: systemic root cause, never human blame
# --------------------------------------------------------------------------- #
_BLAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"user error", r"human error", r"operator (error|mistake)", r"pilot error",
        r"engineer forgot", r"\bforgot to\b", r"pebkac", r"\bidiot",
        r"\bcareless", r"negligen", r"\blazy\b", r"not my fault",
        r"their fault", r"\bwho (messed|broke|forgot)", r"\bblame\b",
    )
)

_SYSTEMIC_KEYWORDS: tuple[str, ...] = (
    "process", "architecture", "tooling", "automation", "monitoring", "alerting",
    "alert", "configuration", "config", "pipeline", "procedure", "system",
    "design", "infrastructure", "policy", "training", "interface", "guardrail",
    "schema", "deploy", "code", "test", "observability", "capacity", "backlog",
    "dashboard", "runbook", "review", "checklist",
)


def _vr005(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR005: root cause must be systemic, never a person.

    Checks (two deterministic sub-checks on ``validated_root_cause``):

    1. no human-blame phrase (``user error``, ``human error``,
       ``operator error/mistake``, ``pilot error``, ``engineer forgot``,
       ``forgot to``, ``pebkac``, ``idiot``, ``careless``,
       ``negligen*``, ``lazy``, ``not my fault``, ``their fault``,
       ``who messed/broke/forgot``, ``blame``);
    2. at least one systemic-domain keyword (process/architecture/
       tooling family: ``process``, ``architecture``, ``tooling``,
       ``automation``, ``monitoring``, ``alerting``, ``configuration``,
       ``pipeline``, ``design``, ``infrastructure``, …).

    Inputs expected: ``trace.validated_root_cause`` (string).

    Passes when: no blame phrase AND >=1 systemic keyword.  Fails when:
    either sub-check trips (blame is checked first).

    Protects: MM005 (5 Whys / Root Cause Analysis) — blameless
    post-mortem culture; a "root cause" naming a person is
    unfalsifiable and fixes nothing.

    Fail examples: ``"The engineer forgot to configure the alert"``
    (blame) / ``"The alert did not fire"`` (no systemic domain).

    Args:
        trace: MM005 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` reporting both sub-checks.
    """
    cause = getattr(trace, "validated_root_cause", "") or ""
    log.debug(
        "VR005 auditing root cause for blame/systemic language",
        extra={"rule_id": "VR005", "cause": _short(cause, 80)},
    )
    blame_hits = [
        m.group(0) for p in _BLAME_PATTERNS if (m := p.search(cause)) is not None
    ]
    if blame_hits:
        message = (
            f"VR005 failed: validated_root_cause blames a human "
            f"({', '.join(sorted(set(blame_hits))[:3])}); it must describe a "
            "process, architecture or tooling flaw."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR005", "reason": _short(message)})
        return False, message
    systemic_hits = [kw for kw in _SYSTEMIC_KEYWORDS if kw in cause.lower()]
    if not systemic_hits:
        message = (
            "VR005 failed: validated_root_cause references no systemic domain "
            "(process/architecture/tooling); it reads like an event, not a flaw."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR005", "reason": _short(message)})
        return False, message
    return True, (
        f"VR005 passed: systemic root cause referencing {systemic_hits[:3]!r} "
        "with no human-blame language."
    )


# --------------------------------------------------------------------------- #
# VR006 — Occam: simplest answer must be the minimal-assumption hypothesis
# --------------------------------------------------------------------------- #
def _vr006(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR006: ``simplest_viable_answer`` must match ``argmin(assumptions)``.

    Checks: the answer the trace commits to must be the hypothesis with
    the fewest ``unverified_assumptions_count`` (Occam's razor made
    mechanical), not a preferred narrative.

    Inputs expected: ``trace.hypotheses`` (list of dicts, each with
    ``explanation`` and ``unverified_assumptions_count``) and
    ``trace.simplest_viable_answer`` (string).

    Matching: the chosen hypothesis explanation must appear inside the
    answer (or vice versa) or share >=2 content tokens with it, so mild
    paraphrases still pass while wrong picks fail.  Malformed hypothesis
    entries (non-dict or missing count) fail closed.

    Passes when: the answer references a minimal-assumption hypothesis.
    Fails when: inputs are missing, a hypothesis entry is malformed, or
    the answer only matches higher-assumption hypotheses.

    Protects: MM006 (Occam's Razor) hypothesis-selection contract.

    Fail example: hypotheses with assumption counts 3 and 1, but the
    answer names the count-3 hypothesis → rejected.

    Args:
        trace: MM006 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` naming the expected explanation.
    """
    hyps = getattr(trace, "hypotheses", None) or []
    answer = (getattr(trace, "simplest_viable_answer", "") or "").lower()
    log.debug(
        "VR006 checking answer against minimal-assumption hypothesis",
        extra={"rule_id": "VR006", "n_hypotheses": len(hyps),
               "answer": _short(answer, 80)},
    )
    if not hyps or not answer:
        message = "VR006 failed: hypotheses or simplest_viable_answer missing."
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR006", "reason": _short(message)})
        return False, message
    parsed: list[tuple[str, float]] = []
    for h in hyps:
        if not isinstance(h, dict) or "unverified_assumptions_count" not in h:
            message = "VR006 failed: malformed hypothesis entry (missing assumption count)."
            log.warning("Validation rule rejected trace",
                        extra={"rule_id": "VR006", "reason": _short(message)})
            return False, message
        parsed.append((str(h.get("explanation", "")), float(h["unverified_assumptions_count"])))
    min_count = min(c for _, c in parsed)
    candidates = [exp for exp, c in parsed if c == min_count]
    for cand in candidates:
        low = cand.lower()
        if low and (low in answer or answer in low or len(_shared_content(low, answer)) >= 2):
            return True, (
                f"VR006 passed: answer matches the minimal-assumption "
                f"hypothesis ({min_count:g} assumption(s)): {cand[:80]!r}."
            )
    message = (
        f"VR006 failed: simplest_viable_answer does not reference the "
        f"minimal-assumption hypothesis ({min_count:g}): {candidates[0][:80]!r}."
    )
    log.warning("Validation rule rejected trace",
                extra={"rule_id": "VR006", "reason": _short(message)})
    return False, message


# --------------------------------------------------------------------------- #
# VR007 — 5x5: conditional emotional budget cap
# --------------------------------------------------------------------------- #
def _vr007(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR007: if it won't matter in 5 years, budget <= 5 minutes.

    Checks: the conditional emotional-budget rule — an event flagged
    ``matters_in_five_years=False`` may allocate at most 5 "emotional
    minutes"; a larger allocation must be consciously justified by
    long-term significance (``matters_in_five_years=True``).

    Inputs expected: ``trace.matters_in_five_years`` (bool, defaulting
    to True when absent) and
    ``trace.emotional_budget_allocated_minutes`` (int, default 0).

    Passes when: the flag is True, or the budget is <= 5.  Fails when:
    flag False AND budget > 5.

    Protects: MM007 (5/5 Rule) emotional-proportionality contract.  The
    schema already caps the field at 5; VR007 guards the conditional
    invariant for dynamic/looser schemas.

    Fail example: ``matters_in_five_years=False`` with 8 allocated
    minutes → rejected.

    Args:
        trace: MM007 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)``.
    """
    matters = bool(getattr(trace, "matters_in_five_years", True))
    minutes = int(getattr(trace, "emotional_budget_allocated_minutes", 0) or 0)
    log.debug(
        "VR007 checking conditional emotional budget",
        extra={"rule_id": "VR007", "matters_in_five_years": matters,
               "minutes": minutes},
    )
    if not matters and minutes > 5:
        message = (
            f"VR007 failed: event does not matter in 5 years yet "
            f"{minutes} emotional minutes were allocated (max 5)."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR007", "reason": _short(message)})
        return False, message
    return True, (
        f"VR007 passed: matters_in_five_years={matters}, "
        f"budget={minutes} minute(s)."
    )


# --------------------------------------------------------------------------- #
# VR008 — Gall's Law: v1 must be minimal (<=3 components)
# --------------------------------------------------------------------------- #
_SEG_SPLIT_RE = re.compile(r",|;|\+|\band\b|\bwith\b|\bplus\b|/|•|·", re.IGNORECASE)
_NUMBERED_RE = re.compile(r"(?:^|\s)\d+\s*[\.\)]\s*")


def _vr008(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR008: ``simple_v1_working_system`` describes <=3 components.

    Checks: Gall's Law — the proposed v1 must be minimal; systems that
    grow working start small, so a first version enumerating many
    components is a design smell the trace must not commit to.

    Component counting (deterministic): split on list delimiters
    (``, ; + and with plus /``) AND count explicit numbered markers
    (``1.``, ``2)``); the larger signal wins.  A component-counting
    classifier / judge upgrade is available for ambiguous prose.

    Inputs expected: ``trace.simple_v1_working_system`` (prose string).

    Passes when: the extracted component count is <= 3.  Fails when:
    the count exceeds 3; the message quotes the first segments.

    Protects: MM008 (Gall's Law) minimal-viable-system contract.

    Fail example: ``"API server, database, cache, queue, workers"`` →
    5 segments → rejected.

    Args:
        trace: MM008 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` including the extracted component list.
    """
    text = getattr(trace, "simple_v1_working_system", "") or ""
    log.debug(
        "VR008 counting v1 components",
        extra={"rule_id": "VR008", "v1_text": _short(text, 80)},
    )
    segments = [s.strip() for s in _SEG_SPLIT_RE.split(text) if s.strip()]
    numbered = len(_NUMBERED_RE.findall(text))
    count = max(len(segments), numbered if numbered > 1 else 0)
    if count > 3:
        message = (
            f"VR008 failed: v1 describes ~{count} components "
            f"({segments[:5]!r}) - not minimal (max 3)."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR008", "reason": _short(message)})
        return False, message
    return True, f"VR008 passed: v1 has ~{count} component(s): {segments!r}."


# --------------------------------------------------------------------------- #
# VR009 — Inversion: 1:1 safeguards vs failure vectors
# --------------------------------------------------------------------------- #
def _vr009(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR009: ``len(preemptive_safeguards) == len(critical_failure_vectors)``.

    Checks: Inversion thinking demands exact 1:1 coverage — every
    identified failure vector gets exactly one safeguard.  Orphan
    vectors are unmitigated risks; orphan safeguards are theatre.

    Inputs expected: ``trace.critical_failure_vectors`` and
    ``trace.preemptive_safeguards`` (lists).

    Passes when: the counts are equal.  Fails when: they differ in
    either direction; the message quotes both counts.

    Protects: MM009 (Inversion) mitigation-completeness contract.

    Fail example: 4 failure vectors vs 3 safeguards → rejected.

    Args:
        trace: MM009 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with both counts.
    """
    vectors = list(getattr(trace, "critical_failure_vectors", None) or [])
    safeguards = list(getattr(trace, "preemptive_safeguards", None) or [])
    log.debug(
        "VR009 comparing vector/safeguard counts",
        extra={"rule_id": "VR009", "n_vectors": len(vectors),
               "n_safeguards": len(safeguards)},
    )
    if len(safeguards) != len(vectors):
        message = (
            f"VR009 failed: {len(vectors)} failure vectors but "
            f"{len(safeguards)} safeguards - every vector needs exactly one."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR009", "reason": _short(message)})
        return False, message
    return True, f"VR009 passed: {len(vectors)} vectors ↔ {len(safeguards)} safeguards."


# --------------------------------------------------------------------------- #
# VR010 — Chesterton's Fence: characterization tests required
# --------------------------------------------------------------------------- #
def _vr010(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR010: ``characterization_tests_required`` must be non-empty.

    Checks: Chesterton's Fence — before modifying an existing system the
    trace must commit to characterization tests that pin down current
    behaviour; "refactor first, understand later" is prohibited.

    Inputs expected: ``trace.characterization_tests_required`` (list of
    test descriptions).

    Passes when: >=1 test is listed.  Fails when: the list is empty or
    missing.

    Protects: MM010 (Chesterton's Fence) change-safety contract — the
    fence stays standing until its purpose is demonstrated.

    Fail example: an empty list → rejected.

    Args:
        trace: MM010 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)``.
    """
    tests = list(getattr(trace, "characterization_tests_required", None) or [])
    log.debug(
        "VR010 checking characterization-test commitment",
        extra={"rule_id": "VR010", "n_tests": len(tests)},
    )
    if not tests:
        message = (
            "VR010 failed: no characterization tests listed - refactoring "
            "before understanding is prohibited."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR010", "reason": _short(message)})
        return False, message
    return True, f"VR010 passed: {len(tests)} characterization test(s) required first."


# --------------------------------------------------------------------------- #
# VR011 — Second-Order: every consequence tier needs >=2 items
# --------------------------------------------------------------------------- #
def _vr011(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR011: all three consequence tiers must contain >=2 items.

    Checks: Second-Order Thinking depth — the first-, second- and
    third-order consequence lists must each carry at least two entries,
    proving "and then what?" was pursued past the obvious first hop.

    Inputs expected: ``trace.first_order_consequences``,
    ``trace.second_order_consequences``,
    ``trace.third_order_consequences`` (lists).

    Passes when: every tier has >=2 items.  Fails when: any tier has 0
    or 1; the message names the shallow tier(s).

    Protects: MM011 (Second-Order Thinking) consequence-depth contract.

    Fail example: first=2, second=2, third=1 → rejected on ``third``.

    Args:
        trace: MM011 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` reporting per-tier counts.
    """
    tiers = {
        "first": list(getattr(trace, "first_order_consequences", None) or []),
        "second": list(getattr(trace, "second_order_consequences", None) or []),
        "third": list(getattr(trace, "third_order_consequences", None) or []),
    }
    log.debug(
        "VR011 checking consequence-tier depth",
        extra={"rule_id": "VR011", "tier_sizes": {k: len(v) for k, v in tiers.items()}},
    )
    shallow = [name for name, items in tiers.items() if len(items) < 2]
    if shallow:
        message = (
            f"VR011 failed: consequence tier(s) {shallow} have <2 items - "
            "'and then what?' was not pursued deep enough."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR011", "reason": _short(message)})
        return False, message
    return True, (
        "VR011 passed: "
        + ", ".join(f"{k}={len(v)}" for k, v in tiers.items())
        + " consequences."
    )


# --------------------------------------------------------------------------- #
# VR012 — Conway: topology must reference bounded contexts / boundaries
# --------------------------------------------------------------------------- #
_BOUNDARY_MARKERS: tuple[str, ...] = (
    "bounded context", "service boundary", "domain", "end-to-end",
    "stream-aligned", "platform team", "owning", "owns", "team owns",
)


def _vr012(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR012: ``proposed_team_topology`` must use boundary language.

    Checks: Conway's Law alignment — the proposal must map teams to
    architecture (bounded contexts / service boundaries), not to org-
    chart conveniences, so communication paths mirror the desired
    system design.

    Heuristic markers (case-insensitive): ``bounded context``,
    ``service boundary``, ``domain``, ``end-to-end``, ``stream-aligned``,
    ``platform team``, ``owning``, ``owns``, ``team owns``.  The spec
    keyword set ('bounded context', 'service boundary', 'domain') is
    extended with the topology vocabulary used by the spec's own gold
    example ('stream-aligned', 'end-to-end', 'platform team', 'owning') —
    otherwise the spec's example would fail its own rule.

    Inputs expected: ``trace.proposed_team_topology`` (string).

    Passes when: >=1 marker appears in the topology text.  Fails when:
    none do.

    Protects: MM012 (Conway's Law) team↔architecture mirroring contract.

    Fail example: ``"Merge the platform and product teams"`` (no
    boundary language) → rejected.

    Args:
        trace: MM012 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` naming matched markers.
    """
    topology = (getattr(trace, "proposed_team_topology", "") or "").lower()
    log.debug(
        "VR012 scanning topology for boundary language",
        extra={"rule_id": "VR012", "topology": _short(topology, 80)},
    )
    matched = [marker for marker in _BOUNDARY_MARKERS if marker in topology]
    if not matched:
        message = (
            "VR012 failed: proposed_team_topology maps no team to a bounded "
            "context / service boundary."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR012", "reason": _short(message)})
        return False, message
    return True, f"VR012 passed: boundary language present {matched[:3]!r}."


# --------------------------------------------------------------------------- #
# VR013 — Cynefin: methodology must match the assigned domain
# --------------------------------------------------------------------------- #
def _vr013(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR013: ``selected_methodology`` must fit ``assigned_domain``.

    Checks: Cynefin sense-making — each domain implies a methodology
    family (Clear → SOP/best-practice, Complicated → analysis/experts,
    Complex → probe/safe-to-fail experiments, Chaotic → act/stabilise);
    applying the wrong family is category error, not strategy.

    Deterministic lookup against ``CYNEFIN_METHODOLOGY_KEYWORDS`` (the
    spec's lookup-table hint; canonical methodology strings are not
    enumerated by the spec, so keyword families approximate the table).

    Inputs expected: ``trace.assigned_domain`` (one of the four Cynefin
    domains), ``trace.selected_methodology`` (free text).

    Passes when: a canonical keyword for the domain matches at a word
    boundary in the methodology text.  Fails when: the domain is
    unknown, or no canonical keyword matches.

    Protects: MM013 (Cynefin) domain↔methodology coherence contract.

    Fail example: domain ``Complex`` with methodology ``"follow the
    standard operating procedure"`` → rejected.

    Args:
        trace: MM013 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with the matched keyword.
    """
    domain = getattr(trace, "assigned_domain", "") or ""
    methodology = (getattr(trace, "selected_methodology", "") or "").lower()
    log.debug(
        "VR013 matching methodology against Cynefin domain",
        extra={"rule_id": "VR013", "domain": _short(domain, 40),
               "methodology": _short(methodology, 80)},
    )
    keywords = CYNEFIN_METHODOLOGY_KEYWORDS.get(domain)
    if keywords is None:
        message = f"VR013 failed: unknown assigned_domain {domain!r}."
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR013", "reason": _short(message)})
        return False, message
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}", methodology):
            return True, (
                f"VR013 passed: methodology {methodology[:60]!r} matches the "
                f"canonical {domain} mapping via keyword {kw!r}."
            )
    message = (
        f"VR013 failed: methodology {methodology[:60]!r} matches no canonical "
        f"{domain} methodology keyword {keywords[:4]!r}."
    )
    log.warning("Validation rule rejected trace",
                extra={"rule_id": "VR013", "reason": _short(message)})
    return False, message


# --------------------------------------------------------------------------- #
# VR014 — Goodhart: counter metric must oppose the gaming vector
# --------------------------------------------------------------------------- #
def _vr014(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR014: counter metric must engage with the gaming vector.

    Checks: Goodhart's Law defence — the metric mandated to counter
    gaming must engage with the anticipated gaming vector; a
    counter-metric about unrelated quantities leaves the exploit open.

    Heuristic: >=1 shared content token (plural-tolerant) between
    ``anticipated_gaming_vector`` and ``mandatory_counter_metric``.  The
    spec itself marks this rule "LLM-as-judge"; the judge upgrade is the
    recommended production configuration here.

    Inputs expected: ``trace.anticipated_gaming_vector`` and
    ``trace.mandatory_counter_metric`` (strings).

    Passes when: >=1 shared concept token.  Fails when: zero overlap.

    Protects: MM014 (Goodhart's Law) anti-gaming contract.

    Fail example: vector ``"agents farm resolved tickets"`` with counter
    metric ``"uptime percentage"`` — no shared token → rejected.

    Args:
        trace: MM014 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with the shared tokens.
    """
    vector = getattr(trace, "anticipated_gaming_vector", "") or ""
    counter = getattr(trace, "mandatory_counter_metric", "") or ""
    log.debug(
        "VR014 measuring vector/counter-metric engagement",
        extra={"rule_id": "VR014", "vector": _short(vector, 80),
               "counter_metric": _short(counter, 80)},
    )
    overlap = _shared_content(vector, counter)
    if not overlap:
        message = (
            "VR014 failed: mandatory_counter_metric engages with none of the "
            "gaming vector's concepts - the measure stays gameable."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR014", "reason": _short(message)})
        return False, message
    return True, (
        f"VR014 passed: counter metric engages gaming vector via {sorted(overlap)[:4]!r}."
    )


# --------------------------------------------------------------------------- #
# VR015 — Map!=Territory: observability covers every drift vector
# --------------------------------------------------------------------------- #
def _vr015(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR015: ``len(observability_requirements) >= len(expected_drift_vectors)``.

    Checks: Map≠Territory — every way the mental model is expected to
    drift from reality must be matched by at least one observability
    signal that would detect the drift; uncovered vectors are silent
    failure modes.

    Inputs expected: ``trace.expected_drift_vectors`` and
    ``trace.observability_requirements`` (lists).

    Passes when: signals >= vectors.  Fails when: signals < vectors;
    the message quotes both counts.

    Protects: MM015 (Map is not the Territory) model-drift contract.

    Fail example: 3 drift vectors vs 2 signals → rejected.

    Args:
        trace: MM015 trace (duck-typed).  model_def: Unused.  guardrails: Unused.

    Returns:
        ``(passed, message)`` with both counts.
    """
    vectors = list(getattr(trace, "expected_drift_vectors", None) or [])
    signals = list(getattr(trace, "observability_requirements", None) or [])
    log.debug(
        "VR015 checking drift-vector observability coverage",
        extra={"rule_id": "VR015", "n_vectors": len(vectors),
               "n_signals": len(signals)},
    )
    if len(signals) < len(vectors):
        message = (
            f"VR015 failed: {len(vectors)} drift vectors but only "
            f"{len(signals)} observability requirement(s) - uncovered drift."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR015", "reason": _short(message)})
        return False, message
    return True, f"VR015 passed: {len(signals)} signals cover {len(vectors)} drift vectors."


# --------------------------------------------------------------------------- #
# VR016 — Universal confidence floor
# --------------------------------------------------------------------------- #
def _vr016(trace: Any, model_def: MentalModelDefinition | None,
           guardrails: GlobalGuardrails) -> tuple[bool, str]:
    """VR016: ``trace.confidence >= model.confidence_threshold``.

    Checks: the universal confidence floor — a trace may only pass when
    its self-assessed confidence meets the owning model's declared
    threshold.  Unregistered traces (``model_def is None``) fall back
    to the engine default ``guardrails.default_confidence_threshold``.

    Inputs expected: ``trace.confidence`` (float) and either a registry
    definition providing ``confidence_threshold`` or the guardrail
    fallback.

    Passes when: ``confidence >= threshold``.  Fails when: below
    threshold; the message quotes both numbers.

    Protects: EVERY mental model (rule type: UNIVERSAL, registered on
    all models) — prevents half-hearted traces from masquerading as
    conclusions.

    Fail example: confidence 0.35 against threshold 0.70 → rejected.

    Args:
        trace: Any mental-model trace (duck-typed).
        model_def: Registry definition providing the threshold.
        guardrails: Global guardrails (default threshold fallback).

    Returns:
        ``(passed, message)`` quoting both numbers.
    """
    confidence = float(getattr(trace, "confidence", 0.0) or 0.0)
    threshold = float(
        model_def.confidence_threshold if model_def is not None
        else guardrails.default_confidence_threshold
    )
    log.debug(
        "VR016 checking confidence floor",
        extra={"rule_id": "VR016", "confidence": confidence, "threshold": threshold},
    )
    if confidence < threshold:
        message = (
            f"VR016 failed: confidence {confidence:.2f} < required {threshold:.2f}."
        )
        log.warning("Validation rule rejected trace",
                    extra={"rule_id": "VR016", "reason": _short(message)})
        return False, message
    return True, f"VR016 passed: confidence {confidence:.2f} >= {threshold:.2f}."


#: Dispatch table: rule_id -> implementation.  Registration here is what
#: makes a rule executable; the registry consistency checker asserts every
#: rule referenced by a model has an entry in this table.
VALIDATORS: dict[str, Callable[[Any, MentalModelDefinition | None, GlobalGuardrails],
                               tuple[bool, str]]] = {
    "VR001": _vr001, "VR002": _vr002, "VR003": _vr003, "VR004": _vr004,
    "VR005": _vr005, "VR006": _vr006, "VR007": _vr007, "VR008": _vr008,
    "VR009": _vr009, "VR010": _vr010, "VR011": _vr011, "VR012": _vr012,
    "VR013": _vr013, "VR014": _vr014, "VR015": _vr015, "VR016": _vr016,
}

_RULE_INDEX: dict[str, dict[str, str]] = {r["rule_id"]: r for r in RULES}


def _dump_trace(trace: Any) -> dict[str, Any]:
    """Serialize a trace for the judge payload (Pydantic or duck-typed).

    The returned dict is embedded into the judge's user prompt; it is
    NOT emitted to logs (payload content stays out of the log stream).

    Args:
        trace: Trace object.

    Returns:
        JSON-safe dict of the trace's fields.
    """
    if isinstance(trace, BaseModel):
        dump = trace.model_dump()
    else:
        dump = {k: v for k, v in vars(trace).items() if not k.startswith("_")}
    log.debug("Trace serialized for judge payload", extra={"n_fields": len(dump)})
    return dump


def run_rule(
    rule_id: str,
    trace: Any,
    model_def: MentalModelDefinition | None,
    guardrails: GlobalGuardrails,
    judge: LLMJudge | None = None,
) -> RuleReport:
    """Execute one validation rule with judge upgrade and fail-closed safety.

    Sequence:
        1. Resolve the deterministic implementation and run it.
        2. If the rule is semantic and a judge is configured, run the
           judge and AND its verdict (veto-only).
        3. Any unexpected rule crash fails the rule (never crashes the
           pipeline) and logs the traceback.

    Fail-closed invariant: an implementation that crashes yields
    ``passed=False`` with an ``errored (fail-closed)`` message — the
    pipeline never advances on a crashed rule.  Judge failures are the
    single deliberate exception: they downgrade to heuristic-only with
    a WARNING (see :mod:`case_engine.validation.llm_judge` for why that
    narrow fail-open path is safe).  The judge, by contrast, can only
    veto: ``judge.passed and not passed`` leaves the failure standing.

    Logging: DEBUG entry (rule id, owning model), DEBUG completion
    (verdict, judge usage), WARNING with the rejection reason whenever
    the deterministic heuristic fails, and WARNING on judge veto or
    judge unavailability.

    Args:
        rule_id: e.g. ``"VR005"``.
        trace: Validated trace object (Pydantic model or duck-typed).
        model_def: Owning model definition (may be ``None`` for VR016 on
            unregistered traces).
        guardrails: Engine guardrails.
        judge: Optional configured judge.

    Returns:
        :class:`RuleReport` with the combined verdict.

    Raises:
        RegistryError: If *rule_id* has no implementation (registry bug);
            logged at ERROR before raising.
    """
    definition = _RULE_INDEX.get(rule_id)
    if definition is None or rule_id not in VALIDATORS:
        log.error("No implementation registered for rule", extra={"rule_id": rule_id})
        raise RegistryError("No implementation registered for rule", rule_id=rule_id)

    log.debug(
        "Rule evaluation started",
        extra={"rule_id": rule_id,
               "model_id": getattr(model_def, "algorithm_id", None)},
    )

    try:
        passed, message = VALIDATORS[rule_id](trace, model_def, guardrails)
    except Exception as exc:
        log.exception("Validation rule crashed", extra={"rule_id": rule_id})
        passed, message = False, f"Rule {rule_id} errored (fail-closed): {exc}"

    judge_used = False
    if judge is not None and getattr(judge, "enabled", False) and rule_id in SEMANTIC_RULE_IDS:
        try:
            verdict = judge.audit(
                definition.get("auditor_prompt") or f"Enforce rule {rule_id}.",
                {"rule": rule_id, "trace": _dump_trace(trace)},
            )
            judge_used = True
            if verdict.passed and not passed:
                # Judge cannot rescue a failed heuristic (veto-only design).
                message += f" | judge agreed to pass but heuristic failed: {verdict.rationale}"
            elif not verdict.passed:
                passed = False
                message += f" | judge VETO: {verdict.rationale}"
                log.warning(
                    "Judge vetoed trace",
                    extra={"rule_id": rule_id, "reason": _short(verdict.rationale)},
                )
            else:
                message += f" | judge concurs: {verdict.rationale}"
        except Exception as exc:
            log.warning(
                "Judge unavailable - falling back to heuristic",
                extra={"rule_id": rule_id, "error": str(exc)},
            )

    log.debug(
        "Rule executed",
        extra={"rule_id": rule_id, "passed": passed, "judge_used": judge_used},
    )
    # G19: message-catalog seam — a non-English catalog may restate the
    # verdict; with no catalog installed this is a no-op and the rule's
    # own English text ships unchanged.
    message = msg(f"vr.{rule_id}.{'pass' if passed else 'fail'}", default=message)
    return RuleReport(
        rule_id=rule_id,
        rule_name=definition["name"],
        rule_type=definition["type"],
        passed=passed,
        message=message,
        judge_used=judge_used,
    )
