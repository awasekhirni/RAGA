# LLM Providers

The engine talks to models through exactly one protocol — a **forced,
schema-gated tool call** — so any provider that can emit a structured tool
call works. Free-text generation is not part of the contract.

---

## 1. The adapter protocol

```python
from case_engine.llm.base import ToolCallRequest, ToolCallResult

class MyProvider:
    name = "my-provider"

    def generate_tool_call(self, request: ToolCallRequest) -> ToolCallResult:
        ...  # call your API, force the tool, extract name + arguments
```

`ToolCallRequest` fields: `system_prompt`, `user_prompt`, `tool_specs`
(OpenAI-shaped), `tool_choice` (default `"required"`), `max_tokens`
(from `guardrails.max_reasoning_tokens`), `metadata` (session/node/model
tags for logging).

`ToolCallResult` fields: `tool_name`, `arguments` (**unvalidated** — the
gate validates), `provider`, `latency_ms`, `raw_content`, `finish_reason`.

Errors must be raised as `LLMAdapterError` / `LLMResponseError` so the
pipeline can route them to the reflexion loop instead of crashing.

## 2. Shipped adapters

| Key | Class | Forcing mechanism | Notes |
|---|---|---|---|
| `mock` | `MockLLMClient` | scripted payloads | Deterministic; auto-scripts from registry examples; `fail_next()` injects failures for tests/demos |
| `openai` | `OpenAIClient` | `tool_choice="required"` | Chat Completions; parses `message.tool_calls[0]` |
| `anthropic` | `AnthropicClient` | `{"type":"tool","name":...}` (single tool) / `{"type":"any"}` | Messages API; parses `tool_use` blocks |
| `ollama` | `OllamaClient` | OpenAI-compat endpoint on `localhost:11434/v1` | Subclass of the OpenAI-compatible client |
| `vllm` | `VLLMClient` | OpenAI-compat endpoint (e.g. `:8000/v1`) | Same base class |

The engine always sends **one tool per request** (one mental model per
call), so Anthropic's per-tool forcing gives the strongest possible
compliance; multi-tool requests (the judge uses one, external stacks may
send many) fall back to `any`.

SDKs are **lazy imports**: the core install needs only `pydantic`; provider
SDKs are extras (`pip install "case-engine[openai]"`, `...[anthropic]`).
Adapter construction raises `LLMProviderUnavailableError` with the exact
install command when an SDK is missing.

## 2b. Transport resilience (v2.2.0, G8) & async (v2.2.0, G5)

**Retry/backoff.** The network adapters wrap exactly their transport call
in `case_engine/llm/retry.py :: call_with_retries`: exponential backoff
with jitter for *transient* failures — HTTP 408/429/5xx and Anthropic's
529 "overloaded", plus timeout/connection-class exceptions — honouring
numeric `Retry-After` headers (clamped to `retry_max_backoff_s`).
Constructor knobs: `retry_attempts=3` (total tries; `1` disables — the
legacy behaviour), `retry_initial_backoff_s=0.5`, `retry_max_backoff_s=8.0`.
After the budget is exhausted the original SDK exception still surfaces at
the single wrapping boundary as `LLMProviderUnavailableError` (a subclass
of `LLMAdapterError`) — no new exception types for callers. **Content
failures are never retried**: a response that parses but lacks a tool call
raises `LLMResponseError` immediately and belongs to the pipeline's
reflexion loop, which exists for exactly that.

**Async (phase 1).** `BaseLLMClient.generate_tool_call_async(request)` is
the async twin of the sync call; the default implementation offloads to a
worker thread via `asyncio.to_thread`, so every adapter in the table above
is async-capable unchanged, and the awaiting event loop is never blocked
by a provider round-trip. End-to-end: `await engine.run_async(query)` —
outcome-identical to `engine.run(query)` (parity-tested). Phase 2
(documented, not yet implemented): native `AsyncOpenAI`/`AsyncAnthropic`
dispatch directly on the loop and streaming with incremental gate
validation. Custom adapters get the async twin for free by implementing
the sync method only.

## 3. Zero-trust note

Provider-side schema enforcement (function calling, JSON mode, grammar
sampling) is treated as a **performance optimization only**. The
enforcement gate (`enforcement/gate.py`) re-validates every payload against
the authoritative typed contract regardless of what the provider claims.
This is what makes the engine safe with `tool_choice`-loose endpoints,
older local models and beta APIs.

## 4. Constrained decoding (Outlines)

The spec's Strategy 4 (token-level grammar constraints) applies to
**self-hosted** models. This engine ships the tool-call gate for hosted
APIs and keeps Outlines intentionally out of the core (heavy dependency,
local-only benefit). To add it, wrap your local sampler in a custom
provider:

```python
import outlines

model = outlines.models.transformers("mistral-7b")
# constrain generation to the tool's JSON schema, then return
# ToolCallResult(tool_name=..., arguments=parsed)
```

…and pass the instance to `CASEEngine(llm_client=...)`. See
`docs/EXTENDING.md` §3.

## 5. Writing a custom provider (gateway / OpenRouter / Bedrock)

`examples/custom_provider.py` shows the minimal adapter (~10 lines). Rules
of thumb:

1. Force the tool (API-native forcing when available).
2. Never parse the model's *content* — only its tool-call arguments.
3. Wrap transport failures in `LLMAdapterError`; "model refused to call the
   tool" is `LLMResponseError` (both trigger reflexion).
4. Never validate the payload yourself — return it raw; the gate owns
   validation (single source of truth).

## 6. Deterministic testing with the Mock

```python
mock = MockLLMClient()                      # auto-scripted from the registry
mock.fail_next(times=1, mode="invalid_args")   # "no_tool_call" | "invalid_args" | "unknown_tool"
engine = CASEEngine(llm_client=mock)
```

- `no_tool_call` — raises `LLMResponseError` (refusal simulation) → reflexion.
- `invalid_args` — serves a schema-violating payload → gate rejection → reflexion.
- `unknown_tool` — hallucinated tool name → gate enforcement fault.

Because the mock is deterministic, the whole 144-test suite and the CLI
demo run offline in under a second.
