"""Unit tests: provider adapters (mock fully; OpenAI/Anthropic via stubs)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from case_engine.exceptions import (
    ConfigurationError,
    LLMAdapterError,
    LLMResponseError,
)
from case_engine.llm import (
    AnthropicClient,
    MockLLMClient,
    OpenAIClient,
    ToolCallRequest,
    create_llm_client,
)
from case_engine.llm.anthropic_client import AnthropicClient as AnthropicClientCls


def _request(tool_name: str = "MyTool") -> ToolCallRequest:
    return ToolCallRequest(
        system_prompt="sys", user_prompt="usr",
        tool_specs=[{"type": "function", "function": {
            "name": tool_name, "description": "d", "parameters": {"type": "object"}}}],
        max_tokens=128,
    )


class TestFactory:
    def test_unknown_provider(self) -> None:
        with pytest.raises(ConfigurationError):
            create_llm_client("skynet")

    def test_mock_provider(self) -> None:
        assert create_llm_client("mock").name == "mock"


class TestMockClient:
    def test_auto_script_from_registry(self) -> None:
        client = MockLLMClient()
        result = client.generate_tool_call(_request("RootCauseAnalysisTrace"))
        assert result.arguments["problem_statement"] == "Dashboard shows stale data"

    def test_no_script_for_unknown_tool(self) -> None:
        with pytest.raises(LLMResponseError):
            MockLLMClient(script={}).generate_tool_call(_request("UnscriptedTool"))

    def test_payload_is_deep_copied(self) -> None:
        client = MockLLMClient(script={"T": {"a": 1}})
        r1 = client.generate_tool_call(_request("T"))
        r1.arguments["a"] = 999
        r2 = client.generate_tool_call(_request("T"))
        assert r2.arguments["a"] == 1

    @pytest.mark.parametrize("mode", ["no_tool_call", "invalid_args", "unknown_tool"])
    def test_failure_modes(self, mode) -> None:
        client = MockLLMClient(script={"T": {"a": 1}}).fail_next(times=1, mode=mode)
        if mode == "no_tool_call":
            with pytest.raises(LLMResponseError):
                client.generate_tool_call(_request("T"))
        elif mode == "unknown_tool":
            result = client.generate_tool_call(_request("T"))
            assert result.tool_name == "DefinitelyNotARealTool"
        else:
            result = client.generate_tool_call(_request("T"))
            assert "__mock_invalid__" in result.arguments

    def test_bad_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            MockLLMClient().fail_next(mode="explode")


class _FakeOpenAICompletions:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    def create(self, **kwargs):
        if self._error:
            raise self._error
        return self._response


def _openai_response(tool=True):
    if not tool:
        msg = SimpleNamespace(tool_calls=None, content="free text refusal")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])
    call = SimpleNamespace(function=SimpleNamespace(name="MyTool",
                                                     arguments=json.dumps({"a": 1})))
    msg = SimpleNamespace(tool_calls=[call], content="")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])


class TestOpenAIAdapter:
    def test_build_kwargs_shape(self) -> None:
        kwargs = OpenAIClient.build_request_kwargs(_request(), model="gpt-4o")
        assert kwargs["model"] == "gpt-4o"
        assert kwargs["tool_choice"] == "required"
        assert kwargs["messages"][0]["role"] == "system"
        assert kwargs["tools"][0]["function"]["name"] == "MyTool"

    def test_parses_tool_call(self) -> None:
        client = OpenAIClient(client=SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeOpenAICompletions(_openai_response(True)))))
        result = client.generate_tool_call(_request())
        assert result.tool_name == "MyTool" and result.arguments == {"a": 1}

    def test_missing_tool_call_raises(self) -> None:
        client = OpenAIClient(client=SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeOpenAICompletions(_openai_response(False)))))
        with pytest.raises(LLMResponseError):
            client.generate_tool_call(_request())

    def test_transport_error_wrapped(self) -> None:
        client = OpenAIClient(client=SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeOpenAICompletions(
                error=RuntimeError("boom")))))
        with pytest.raises(LLMAdapterError):
            client.generate_tool_call(_request())


class TestAnthropicAdapter:
    def test_convert_tools(self) -> None:
        converted = AnthropicClientCls._convert_tools(
            [{"type": "function", "function": {
                "name": "T", "description": "d", "parameters": {"type": "object"}}}])
        assert converted == [{"name": "T", "description": "d",
                              "input_schema": {"type": "object"}}]

    def test_single_tool_forces_that_tool(self) -> None:
        kwargs = AnthropicClientCls.build_request_kwargs(_request("SoloTool"))
        assert kwargs["tool_choice"] == {"type": "tool", "name": "SoloTool"}

    def test_multiple_tools_force_any(self) -> None:
        request = _request()
        request.tool_specs.append({"type": "function", "function": {
            "name": "Other", "parameters": {}}})
        kwargs = AnthropicClientCls.build_request_kwargs(request)
        assert kwargs["tool_choice"] == {"type": "any"}

    def test_parses_tool_use_block(self) -> None:
        block = SimpleNamespace(type="tool_use", name="MyTool", input={"b": 2})
        text = SimpleNamespace(type="text", text="thinking...")
        response = SimpleNamespace(content=[text, block], stop_reason="tool_use")
        client = AnthropicClient(client=SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: response)))
        result = client.generate_tool_call(_request())
        assert result.tool_name == "MyTool" and result.arguments == {"b": 2}

    def test_missing_tool_use_raises(self) -> None:
        text = SimpleNamespace(type="text", text="I will not comply")
        response = SimpleNamespace(content=[text], stop_reason="end_turn")
        client = AnthropicClient(client=SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: response)))
        with pytest.raises(LLMResponseError):
            client.generate_tool_call(_request())
