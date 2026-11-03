"""G27 — recorded-cassette provider contract tests.

The OpenAI/Anthropic adapters are normally tested against hand-built
stubs; SDK shape drift (both SDKs churn) would pass CI and fail in the
field.  These tests pin the adapters to **recorded wire cassettes**
(``tests/cassettes/*.json``): canonical provider response envelopes with
a declared ``meta.pinned_sdk`` and an explicit refresh policy.

Three layers of contract:

1. **Envelope contract** — the cassette's response must carry exactly
   the envelope fields the adapter relies on (``choices[].message.tool_calls``,
   ``usage.prompt_tokens``, ...).  A provider wire change fails here.
2. **Adapter replay** — a stub client built from the cassette is driven
   through ``generate_tool_call``; the parsed :class:`ToolCallResult`
   must match the cassette's ``expected`` projection, and the degradation
   variants must raise the documented error classes.
3. **Real-SDK validation** (skipped when the SDK is not installed) —
   the cassette payload is parsed by the *actual* SDK response model
   (``openai.types.chat.ChatCompletion`` /
   ``anthropic.types.Message``).  This is the drift alarm: if the SDK
   renames or removes a field the engine depends on, refreshing the
   cassette fails here instead of in production.

Refresh procedure: update the cassette against the live wire format for
the new SDK version, run this module, and adjust ``meta.pinned_sdk``.
No network access happens inside the tests.

Cross-references: ``case_engine/llm/openai_client.py``,
``case_engine/llm/anthropic_client.py``, ``docs/GAP_ANALYSIS.md`` §G27,
``docs/LLM_PROVIDERS.md``.
"""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from case_engine.exceptions import LLMAdapterError, LLMResponseError
from case_engine.llm.anthropic_client import AnthropicClient
from case_engine.llm.base import ToolCallRequest
from case_engine.llm.openai_client import OpenAIClient

CASETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes"
OPENAI_CASSETTE = CASETTE_DIR / "openai_chat_completions_cassette.json"
ANTHROPIC_CASSETTE = CASETTE_DIR / "anthropic_messages_cassette.json"

#: Envelope fields the OpenAI adapter path actually touches.
_OPENAI_ENVELOPE = {
    "id": str,
    "object": str,
    "model": str,
    "choices": list,
    "usage": dict,
}
_ANTHROPIC_ENVELOPE = {
    "id": str,
    "type": str,
    "role": str,
    "model": str,
    "content": list,
    "stop_reason": str,
    "usage": dict,
}


def _load_cassette(path: Path) -> dict[str, Any]:
    """Load a cassette JSON document.

    Args:
        path: Cassette file path.

    Returns:
        Parsed cassette dict.

    Raises:
        AssertionError: When the cassette lacks the required top-level
            sections (authoring error, not a runtime condition).
    """
    cassette = json.loads(path.read_text(encoding="utf-8"))
    for section in ("meta", "response", "expected"):
        assert section in cassette, f"{path.name} missing '{section}' section"
    assert "pinned_sdk" in cassette["meta"], f"{path.name} missing meta.pinned_sdk"
    assert "refresh_policy" in cassette["meta"]["recorded"]
    return cassette


def _ns(obj: Any, _key: str | None = None) -> Any:
    """Recursively convert dicts/lists into attribute-access objects.

    Mirrors how the provider SDKs surface parsed JSON payloads (object
    attributes with dict/list containers) so cassettes can drive the
    adapters without the real SDK installed.  ``tool_use.input`` stays a
    plain ``dict`` — the Anthropic SDK models it as one and the adapter
    relies on that.
    """
    if _key == "input" and isinstance(obj, dict):
        return obj
    if isinstance(obj, dict):
        return SimpleNamespace(**{key: _ns(value, _key=key) for key, value in obj.items()})
    if isinstance(obj, list):
        return [_ns(item) for item in obj]
    return obj


def _make_request() -> ToolCallRequest:
    """Return the canonical single-tool request used by every replay."""
    return ToolCallRequest(
        system_prompt="system",
        user_prompt="user",
        tool_specs=[{
            "type": "function",
            "function": {
                "name": "RootCauseAnalysisTrace",
                "description": " RCA trace",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        tool_choice="required",
        max_tokens=512,
        metadata={"session_id": "cassette", "node": "node_4_generate"},
    )


class _StubOpenAIClient:
    """Minimal stand-in for the OpenAI SDK client (cassette-driven)."""

    def __init__(self, response: dict[str, Any]) -> None:
        """Store the canned response payload."""
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: _ns(response))
        )


class _StubAnthropicClient:
    """Minimal stand-in for the Anthropic SDK client (cassette-driven)."""

    def __init__(self, response: dict[str, Any]) -> None:
        """Store the canned response payload."""
        self.messages = SimpleNamespace(create=lambda **kwargs: _ns(response))


# --------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------- #


def test_openai_cassette_envelope_contract() -> None:
    """The recorded envelope still carries every field the adapter uses."""
    cassette = _load_cassette(OPENAI_CASSETTE)
    response = cassette["response"]
    for field, expected_type in _OPENAI_ENVELOPE.items():
        assert field in response, f"OpenAI wire drift: '{field}' vanished from the envelope"
        assert isinstance(response[field], expected_type), f"OpenAI '{field}' type drift"

    choice = response["choices"][0]
    for field in ("index", "message", "finish_reason"):
        assert field in choice, f"OpenAI choice drift: '{field}' missing"
    message = choice["message"]
    assert message["role"] == "assistant"
    tool_call = message["tool_calls"][0]
    assert tool_call["type"] == "function"
    assert isinstance(tool_call["function"]["arguments"], str), (
        "OpenAI tool-call arguments must stay a JSON *string* on the wire"
    )
    assert tool_call["function"]["name"]
    for field in ("prompt_tokens", "completion_tokens"):
        assert field in response["usage"], f"OpenAI usage drift: '{field}' missing"


def test_openai_adapter_replays_recorded_cassette() -> None:
    """The adapter's parsing of the recorded response matches ``expected``."""
    cassette = _load_cassette(OPENAI_CASSETTE)
    client = OpenAIClient(
        model="gpt-4o-2024-08-06", api_key="test", retry_attempts=1,
        client=_StubOpenAIClient(cassette["response"]),
    )
    result = client.generate_tool_call(_make_request())
    expected = cassette["expected"]
    assert result.tool_name == expected["tool_name"]
    assert result.arguments == expected["arguments"]
    assert result.finish_reason == expected["finish_reason"]
    assert result.usage == expected["usage"]
    assert result.provider == "openai"


@pytest.mark.parametrize(
    "variant_name", ["no_tool_call_degradation", "tool_call_arguments_not_json"]
)
def test_openai_cassette_variants_raise_documented_errors(variant_name: str) -> None:
    """Degradation variants raise exactly the documented error classes."""
    cassette = _load_cassette(OPENAI_CASSETTE)
    variant = next(v for v in cassette["variants"] if v["name"] == variant_name)
    error_name = variant["expect"]["error"]
    error_class = getattr(import_module("case_engine.exceptions"), error_name)

    client = OpenAIClient(
        model="gpt-4o-2024-08-06", api_key="test", retry_attempts=1,
        client=_StubOpenAIClient(variant["response"]),
    )
    with pytest.raises(error_class):
        client.generate_tool_call(_make_request())


def test_openai_cassette_validates_against_real_sdk() -> None:
    """When the real SDK is installed, the cassette must parse as-is."""
    pytest.importorskip("openai", reason="openai SDK not installed (optional extra)")
    from openai.types.chat import ChatCompletion

    cassette = _load_cassette(OPENAI_CASSETTE)
    parsed = ChatCompletion.model_validate(cassette["response"])
    assert parsed.choices[0].message.tool_calls is not None
    assert parsed.choices[0].message.tool_calls[0].function.name == (
        cassette["expected"]["tool_name"]
    )


# --------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------- #


def test_anthropic_cassette_envelope_contract() -> None:
    """The recorded envelope still carries every field the adapter uses."""
    cassette = _load_cassette(ANTHROPIC_CASSETTE)
    response = cassette["response"]
    for field, expected_type in _ANTHROPIC_ENVELOPE.items():
        assert field in response, f"Anthropic wire drift: '{field}' vanished"
        assert isinstance(response[field], expected_type), f"Anthropic '{field}' type drift"

    tool_use_blocks = [b for b in response["content"] if b["type"] == "tool_use"]
    assert tool_use_blocks, "cassette lost its tool_use block"
    block = tool_use_blocks[0]
    assert isinstance(block["input"], dict), "tool_use input must stay a JSON object"
    for field in ("input_tokens", "output_tokens"):
        assert field in response["usage"], f"Anthropic usage drift: '{field}' missing"


def test_anthropic_adapter_replays_recorded_cassette() -> None:
    """The adapter's parsing of the recorded response matches ``expected``."""
    cassette = _load_cassette(ANTHROPIC_CASSETTE)
    client = AnthropicClient(
        model="claude-sonnet-4-20250514", api_key="test", retry_attempts=1,
        client=_StubAnthropicClient(cassette["response"]),
    )
    result = client.generate_tool_call(_make_request())
    expected = cassette["expected"]
    assert result.tool_name == expected["tool_name"]
    assert result.arguments == expected["arguments"]
    assert result.finish_reason == expected["finish_reason"]
    # The adapter normalizes input/output tokens into the G7 shape.
    assert result.usage == expected["usage"]
    assert result.provider == "anthropic"


def test_anthropic_cassette_no_tool_use_variant_raises() -> None:
    """A prose-only degradation raises ``LLMResponseError`` (reflexion-routed)."""
    cassette = _load_cassette(ANTHROPIC_CASSETTE)
    variant = cassette["variants"][0]
    assert variant["expect"]["error"] == "LLMResponseError"
    client = AnthropicClient(
        model="claude-sonnet-4-20250514", api_key="test", retry_attempts=1,
        client=_StubAnthropicClient(variant["response"]),
    )
    with pytest.raises(LLMResponseError):
        client.generate_tool_call(_make_request())


def test_anthropic_cassette_validates_against_real_sdk() -> None:
    """When the real SDK is installed, the cassette must parse as-is."""
    pytest.importorskip("anthropic", reason="anthropic SDK not installed (optional extra)")
    from anthropic.types import Message

    cassette = _load_cassette(ANTHROPIC_CASSETTE)
    parsed = Message.model_validate(cassette["response"])
    tool_use = [b for b in parsed.content if getattr(b, "type", "") == "tool_use"]
    assert tool_use and tool_use[0].name == cassette["expected"]["tool_name"]


# --------------------------------------------------------------------- #
# Adapter error normalization (transport-level, provider-agnostic)
# --------------------------------------------------------------------- #


def test_openai_transport_failure_is_normalized() -> None:
    """A raising transport surfaces as ``LLMProviderUnavailableError``."""
    def _raise_runtime(**kwargs: Any) -> None:
        raise RuntimeError("connection reset")

    class _FailingClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=_raise_runtime))

    client = OpenAIClient(
        model="gpt-4o", api_key="test", retry_attempts=1,
        client=_FailingClient(),
    )
    with pytest.raises(LLMAdapterError, match="OpenAI request failed"):
        client.generate_tool_call(_make_request())
