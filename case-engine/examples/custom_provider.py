"""Example 4 — plugging in your own LLM provider.

Run:  python examples/custom_provider.py

Any object implementing ``generate_tool_call(request) -> ToolCallResult``
can drive the engine — here a tiny "echo" provider that always returns
the registry gold example for whichever tool it is asked to call.  This
is the integration seam for internal gateways, OpenRouter, Bedrock,
self-hosted models, etc.
"""

from __future__ import annotations

from case_engine import CASEEngine, EngineConfig
from case_engine.audit import InMemoryLedger
from case_engine.llm.base import ToolCallRequest, ToolCallResult
from case_engine.registry import CognitiveRegistry


class EchoGoldProvider:
    """A minimal custom provider: answers every tool call with the gold example."""

    name = "echo-gold"

    def __init__(self) -> None:
        """Preload one gold example per tool name."""
        self._script = {
            m.tool_name: dict(m.examples[0])
            for m in CognitiveRegistry.load_default().all_models()
            if m.examples
        }

    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        """Serve the scripted payload for the requested tool.

        Args:
            request: The engine's schema-gated request.

        Returns:
            A tool call result (payload validated downstream by the gate).
        """
        tool = request.tool_specs[0]["function"]["name"]
        return ToolCallResult(
            tool_name=tool,
            arguments=self._script[tool],
            provider=self.name,
        )


def main() -> None:
    """Run the engine with the custom provider."""
    engine = CASEEngine(
        config=EngineConfig(ledger_backend="memory"),
        llm_client=EchoGoldProvider(),   # <- your adapter, no subclassing needed
        ledger=InMemoryLedger(),
    )
    result = engine.run("Why are users churning?")
    print(result.final_response)


if __name__ == "__main__":
    main()
