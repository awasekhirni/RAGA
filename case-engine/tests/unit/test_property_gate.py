"""G22 — property-based and fuzz tests for the trust boundary.

The SchemaGate is the security boundary of the C.A.S.E. engine and
``canonical_json``/``compute_event_hash`` are the tamper-evidence
primitives; example-based tests alone cannot establish the "never
approves an invalid payload" claim.  This suite generates adversarial
tool-call arguments with **Hypothesis** (extra keys, wrong types, deep
nesting, unicode abuse, extreme scalars) and asserts the structural
invariants:

* the gate never approves a payload that is not schema-conformant,
* the gate never crashes on arbitrary input (only unknown tool names
  raise, and only :class:`ToolCallRejectedError`),
* approval implies the payload carries exactly the contract's fields
  (``extra="forbid"`` semantics hold under fuzzing),
* single-field mutations of a gold payload are always rejected,
* ``canonical_json`` is deterministic, order-insensitive and
  round-trip-stable, and ``compute_event_hash`` changes whenever any
  chain input changes (hash stability / avalanche sanity).

The suite is deterministic in CI (``derandomize`` profile) and skips
gracefully when ``hypothesis`` is not installed — the engine itself
keeps its zero-runtime-dependency promise (hypothesis is a ``dev``
extra only).

Cross-references: ``case_engine/enforcement/gate.py`` (Strategy 1),
``case_engine/audit/base.py`` (chain primitives),
``docs/GAP_ANALYSIS.md`` §G22.
"""

from __future__ import annotations

import copy
import json

import pytest

hypothesis = pytest.importorskip(
    "hypothesis",
    reason="G22 property suite requires hypothesis (pip install 'case-engine[dev]' + hypothesis)",
)

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from case_engine.audit.base import GENESIS_HASH, canonical_json, compute_event_hash
from case_engine.enforcement.gate import SchemaGate
from case_engine.exceptions import ToolCallRejectedError
from case_engine.registry import CognitiveRegistry
from case_engine.schemas.mental_models import SCHEMA_BY_MODEL_ID

# --------------------------------------------------------------------- #
# Deterministic CI profile: same examples every run, no wall-clock
# deadline flakiness.  50 examples per property keeps the whole module
# well under 30 s while still exploring thousands of payload shapes.
# --------------------------------------------------------------------- #
settings.register_profile(
    "case_ci",
    max_examples=50,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("case_ci")

#: Tool contracts exercised by the mutation properties (stable, public
#: entries of the default registry with rich string fields).
_FUZZ_TOOL_NAMES = [
    "RootCauseAnalysisTrace",
    "OODAExecutionTrace",
    "ComplexityDomainClassifier",
]

#: Unicode abuse alphabet: zero-width joiners, bidi overrides, RTL marks,
#: homoglyph look-alikes and control characters — the shapes an injected
#: payload uses to smuggle directives past naive filters.
_ABUSE_CHARS = "\u200b\u200c\u200d\u202e\u202d\u0000\u0007\uff2f\u05d0\u0660"
_ABUSE_ALPHABET = st.characters(blacklist_characters=_ABUSE_CHARS, blacklist_categories=("Cs",))

#: The exact abuse characters, exercised as *content* (never surrogates,
#: which Pydantic/JSON reject by design).
_ABUSE_TEXT = st.text(alphabet=_ABUSE_CHARS, min_size=1, max_size=8)


def _json_scalars() -> st.SearchStrategy:
    """Return adversarial JSON scalar values (extremes included)."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(alphabet=_ABUSE_ALPHABET, max_size=64),
        _ABUSE_TEXT,
    )


def _json_values() -> st.SearchStrategy:
    """Return adversarial JSON values: deep nesting, mixed-type lists."""
    return st.recursive(
        _json_scalars(),
        lambda children: st.one_of(
            st.lists(children, max_size=6),
            st.dictionaries(st.text(alphabet=_ABUSE_ALPHABET, max_size=16), children, max_size=6),
        ),
        max_leaves=30,
    )


#: Arbitrary adversarial tool-call argument dicts.
ADVERSARIAL_ARGS = st.dictionaries(
    st.text(alphabet=_ABUSE_ALPHABET, max_size=24), _json_values(), max_size=12
)

#: Deeply nested payloads (50 levels) that naive recursive validators
#: choke on; the gate must simply reject them via Pydantic.
DEEP_NESTED_ARGS = st.dictionaries(
    st.text(alphabet=_ABUSE_ALPHABET, max_size=8),
    _json_values(),
    min_size=1,
    max_size=3,
)


def _contract_for(registry: CognitiveRegistry, tool_name: str) -> type:
    """Return the Pydantic contract class registered under *tool_name*."""
    model = registry.get_by_tool_name(tool_name)
    return SCHEMA_BY_MODEL_ID[model.algorithm_id]


# --------------------------------------------------------------------- #
# Property 1 — arbitrary garbage is either rejected or provably
# conformant; the gate itself never crashes.
# --------------------------------------------------------------------- #
@given(tool_name=st.sampled_from(_FUZZ_TOOL_NAMES), arguments=ADVERSARIAL_ARGS)
def test_gate_never_crashes_and_approval_implies_conformance(
    registry: CognitiveRegistry, tool_name: str, arguments: dict
) -> None:
    """Fuzzed payloads either get ``approved=False`` or fully conform.

    Property: for a *known* tool name, ``SchemaGate.check`` returns a
    :class:`GateDecision` for **any** argument dict (never raises), and
    whenever it approves, the payload carries exactly the contract's
    property names and every required field — i.e. ``extra="forbid"``
    holds under fuzzing, not just on curated examples.
    """
    gate = SchemaGate(registry)
    decision = gate.check(tool_name, arguments)
    assert decision.tool_name == tool_name
    if decision.approved:
        contract = _contract_for(registry, tool_name)
        schema = contract.model_json_schema()
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert set(arguments) <= properties, "approval with unknown/extra keys"
        assert required <= set(arguments), "approval with missing required fields"
        # Approval is reproducible: re-checking the parsed projection
        # must approve again (the gate is deterministic).
        assert gate.check(tool_name, decision.parsed).approved


@given(arguments=DEEP_NESTED_ARGS)
def test_gate_rejects_deep_nesting(registry: CognitiveRegistry, arguments: dict) -> None:
    """50-level nested payloads never slip through as a valid trace."""
    payload = dict(arguments)
    cursor = payload.setdefault("problem_statement", {})
    for level in range(50):
        cursor["deeper"] = {}
        cursor = cursor["deeper"]
    gate = SchemaGate(registry)
    decision = gate.check("RootCauseAnalysisTrace", payload)
    assert decision.approved is False
    assert decision.errors


# --------------------------------------------------------------------- #
# Property 2 — single-field mutations of a gold payload are rejected.
# --------------------------------------------------------------------- #
def _string_props(contract: type) -> list[str]:
    """Return property names whose schema type is exactly ``string``."""
    schema = contract.model_json_schema()
    names = []
    for prop, spec in schema.get("properties", {}).items():
        if spec.get("type") == "string" or (
            "anyOf" in spec
            and all(t.get("type") in ("string", "null") for t in spec["anyOf"])
        ):
            names.append(prop)
    return names


@given(
    tool_name=st.sampled_from(_FUZZ_TOOL_NAMES),
    mode=st.sampled_from(["extra_key", "drop_required", "type_flip"]),
    data=st.data(),
)
def test_mutated_gold_payload_is_never_approved(
    registry: CognitiveRegistry, gold_payloads: dict, tool_name: str, mode: str, data: st.DataObject
) -> None:
    """Adding, dropping or type-flipping one field kills the approval.

    This is the adversarial core of G22: start from a *known-good*
    payload and apply one mutation — the class of manipulation a
    prompt-injected LLM would produce — and assert the gate always
    rejects it.
    """
    model = registry.get_by_tool_name(tool_name)
    gold = copy.deepcopy(gold_payloads[model.algorithm_id])
    contract = _contract_for(registry, tool_name)
    schema = contract.model_json_schema()
    required = list(schema.get("required", []))
    payload = copy.deepcopy(gold)

    if mode == "extra_key":
        payload[data.draw(st.text(alphabet="abcdefgh_", min_size=1, max_size=8))] = data.draw(
            _json_values()
        )
    elif mode == "drop_required":
        target = data.draw(st.sampled_from(required)) if required else None
        if target is None:  # contract without required fields: nothing to drop
            return
        payload.pop(target, None)
    else:  # type_flip on a string field actually present in the payload
        flip_targets = [k for k in _string_props(contract) if k in payload]
        if not flip_targets:
            return
        target = data.draw(st.sampled_from(flip_targets))
        payload[target] = data.draw(st.lists(_json_values(), min_size=1, max_size=3))

    decision = SchemaGate(registry).check(tool_name, payload)
    assert decision.approved is False, f"mutation {mode} on {tool_name} was approved"


# --------------------------------------------------------------------- #
# Property 3 — unknown tool names always raise the enforcement fault.
# --------------------------------------------------------------------- #
@given(tool_name=st.text(alphabet=_ABUSE_ALPHABET, min_size=0, max_size=40), arguments=ADVERSARIAL_ARGS)
def test_unknown_tool_always_raises_enforcement_fault(
    registry: CognitiveRegistry, tool_name: str, arguments: dict
) -> None:
    """Any unregistered tool name raises ``ToolCallRejectedError``."""
    real_names = {m.tool_name for m in registry.all_models()}
    if tool_name in real_names:
        return  # property only covers unknown names
    with pytest.raises(ToolCallRejectedError):
        SchemaGate(registry).check(tool_name, arguments)


# --------------------------------------------------------------------- #
# Property 4 — canonical_json determinism / round-trip / order-blindness.
# --------------------------------------------------------------------- #
@given(value=_json_values())
def test_canonical_json_round_trip_and_determinism(value) -> None:
    """``canonical_json`` is deterministic, order-blind and round-trips."""
    as_dict = {"k": value}
    first = canonical_json(as_dict)
    second = canonical_json(as_dict)
    assert first == second
    # Key order must not affect the canonical form (hash stability).
    assert canonical_json({k: v for k, v in reversed(list(as_dict.items()))}) == first
    # Round-trip identity: parse-back equals the original value (NaN ==
    # NaN compares True through the identity shortcut on floats; list /
    # dict containers compare structurally).
    loaded = json.loads(first)["k"]
    assert json.dumps(loaded, sort_keys=True) == json.dumps(value, sort_keys=True)
    # Non-ASCII printable content is preserved verbatim
    # (ensure_ascii=False contract; control characters are JSON-escaped).
    if isinstance(value, str) and all(0x20 <= ord(ch) != 0x7F for ch in value):
        assert value in first


@given(a=_json_values(), b=_json_values())
def test_canonical_json_distinguishes_distinct_values(a, b) -> None:
    """Distinct JSON values never collide in canonical form."""
    if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True):
        return
    assert canonical_json({"v": a}) != canonical_json({"v": b})


# --------------------------------------------------------------------- #
# Property 5 — chain hash stability and avalanche sanity.
# --------------------------------------------------------------------- #
@given(
    seq=st.integers(min_value=1, max_value=2**31),
    timestamp=st.text(alphabet="0123456789T:.-+Z", min_size=20, max_size=32),
    event_type=st.sampled_from(["TRACE_GENERATED", "TRACE_REJECTED", "SESSION_START"]),
    session_id=st.text(alphabet="abcdef0123456789-", min_size=8, max_size=36),
    node=st.sampled_from(["node_4_generate", "node_5_validate", "engine"]),
    payload=st.dictionaries(st.text(alphabet="abcdefgh_", max_size=10), _json_values(), max_size=5),
)
def test_event_hash_is_stable_and_sensitive(
    seq: int, timestamp: str, event_type: str, session_id: str, node: str, payload: dict
) -> None:
    """``compute_event_hash`` is deterministic and input-sensitive."""
    kwargs = dict(
        prev_hash=GENESIS_HASH, seq=seq, timestamp=timestamp, event_type=event_type,
        session_id=session_id, node=node, payload=payload,
    )
    h1 = compute_event_hash(**kwargs)
    h2 = compute_event_hash(**kwargs)
    assert h1 == h2
    assert len(h1) == 64
    # Any material change must move the hash (deterministic flip of the
    # payload preserves the "different input -> different hash" sanity).
    flipped = dict(payload)
    flipped["__mutation__"] = 1
    assert compute_event_hash(
        prev_hash=GENESIS_HASH, seq=seq, timestamp=timestamp, event_type=event_type,
        session_id=session_id, node=node, payload=flipped,
    ) != h1 or payload == {"__mutation__": 1}


def test_canonical_json_circular_reference_raises() -> None:
    """Circular references raise ``ValueError`` (documented failure mode)."""
    circular: dict = {"a": 1}
    circular["self"] = circular
    with pytest.raises(ValueError):
        canonical_json(circular)
