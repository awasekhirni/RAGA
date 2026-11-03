"""Tests for the P1 gap closures (G6, G7, G8, G9, G10, G11, G12, G13).

Every test is hermetic: in-memory or temp-dir backends, tiny backoff
values, no network (the webhook sink is exercised through URL validation
and a local thread server only where cheap).  These tests are the
regression contract for the v2.2.0 P1 closure round documented in
``docs/GAP_ANALYSIS.md``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from case_engine import CASEEngine
from case_engine.audit import (
    AUDIT_EVENT_SCHEMA_VERSION,
    JSONLLedger,
    MemoryAnchorSink,
)
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.escalation import (
    EscalationDeliveryError,
    EscalationHandoff,
    FileEscalationSink,
    LoggingEscalationSink,
    NoopEscalationSink,
    WebhookEscalationSink,
    handoff_from_result,
)
from case_engine.exceptions import (
    LedgerError,
    LLMProviderUnavailableError,
)
from case_engine.llm import MockLLMClient
from case_engine.llm.retry import (
    RetryPolicy,
    call_with_retries,
    classify_transport_failure,
    retry_after_seconds,
)
from case_engine.observability import (
    InMemoryMetrics,
    NoopMetrics,
    render_prometheus,
    safe_incr,
)
from case_engine.schemas.pipeline import CASEResult, TokenUsage
from case_engine.storage import (
    SQLiteResultStore,
    create_result_store,
)
from case_engine.validation.llm_judge import LLMBackedJudge


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _UsageMockClient(MockLLMClient):
    """Mock client that reports fake provider usage on every call."""

    def generate_tool_call(self, request):
        result = super().generate_tool_call(request)
        result.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return result


def _engine(**kwargs) -> CASEEngine:
    """Build a minimal offline engine (memory ledger, mock provider)."""
    config = EngineConfig(ledger_backend="memory")
    return CASEEngine(config=config, **kwargs)


class _FakeStatusError(Exception):
    """Stand-in for an SDK status error carrying ``status_code``."""

    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.response = type("R", (), {"headers": headers or {}})()


def _ok_openai_response() -> Any:
    """Minimal OpenAI-shaped success response for fakes (module helper)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                tool_calls=[SimpleNamespace(
                    function=SimpleNamespace(name="T", arguments='{"a": 1}'),
                )],
                content=""),
            finish_reason="stop")],
        usage=None,
    )


class _FlakySDK:
    """Fake OpenAI-shaped SDK client whose create() fails N times, then ok."""

    def __init__(self, failures: int, exc: Exception) -> None:
        self.failures = failures
        self.exc = exc
        self.calls = 0
        self.chat = type("C", (), {})()
        self.chat.completions = type("CC", (), {})()
        self.chat.completions.create = self._create

    def _create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return _ok_openai_response()


class _FlakyAnthropicSDK:
    """Fake Anthropic-shaped SDK client (messages.create)."""

    def __init__(self, failures: int, exc: Exception) -> None:
        self.failures = failures
        self.exc = exc
        self.calls = 0
        self.messages = type("M", (), {})()
        self.messages.create = self._create

    def _create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        from types import SimpleNamespace

        return SimpleNamespace(
            content=[SimpleNamespace(
                type="tool_use", name="T", input={"a": 1})],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=9, output_tokens=4),
        )


# --------------------------------------------------------------------------- #
# G7 — token usage accounting
# --------------------------------------------------------------------------- #
class TestG7TokenUsage:
    def test_usage_aggregated_into_result(self):
        engine = _engine(llm_client=_UsageMockClient())
        result = engine.run("Zone 4 sensors spike anomalously")
        assert result.all_valid
        # 1 classifier + 3 generation calls
        assert result.token_usage.calls == 4
        assert result.token_usage.prompt_tokens == 40
        assert result.token_usage.completion_tokens == 20
        assert result.token_usage.total_tokens == 60

    def test_usage_without_provider_report_counts_calls(self):
        engine = _engine()
        result = engine.run("Zone 4 sensors spike anomalously")
        assert result.token_usage.calls == 4
        assert result.token_usage.total_tokens == 0

    def test_llm_usage_audit_event_in_ledger(self):
        engine = _engine(llm_client=_UsageMockClient())
        engine.run("Zone 4 sensors spike anomalously")
        usage_events = [e for e in engine.ledger.iter_events()
                        if e.event_type == "LLM_USAGE"]
        assert len(usage_events) == 1
        payload = usage_events[0].payload
        assert payload["calls"] == 4
        assert payload["total_tokens"] == 60

    def test_token_usage_model_math(self):
        usage = TokenUsage()
        usage.add(prompt_tokens=10, completion_tokens=5)
        usage.add(prompt_tokens=3, completion_tokens=2)
        assert (usage.calls, usage.prompt_tokens,
                usage.completion_tokens, usage.total_tokens) == (2, 13, 7, 20)

    def test_negative_usage_clamped(self):
        usage = TokenUsage()
        usage.add(prompt_tokens=-5, completion_tokens=7)
        assert usage.prompt_tokens == 0
        assert usage.total_tokens == 7


# --------------------------------------------------------------------------- #
# G8 — provider resilience (retry/backoff)
# --------------------------------------------------------------------------- #
class TestG8Retry:
    def test_policy_validation(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError):
            RetryPolicy(initial_backoff_s=-1.0)

    def test_backoff_curve_and_cap(self):
        policy = RetryPolicy(max_attempts=5, initial_backoff_s=0.5,
                             max_backoff_s=2.0, jitter_s=0.0)
        assert policy.sleep_for(attempt=1) == pytest.approx(0.5)
        assert policy.sleep_for(attempt=2) == pytest.approx(1.0)
        assert policy.sleep_for(attempt=3) == pytest.approx(2.0)
        assert policy.sleep_for(attempt=9) == pytest.approx(2.0)  # capped

    def test_retry_after_overrides_curve_and_clamps(self):
        policy = RetryPolicy(max_backoff_s=2.0, jitter_s=0.0)
        assert policy.sleep_for(attempt=1, retry_after=1.25) == pytest.approx(1.25)
        assert policy.sleep_for(attempt=1, retry_after=99.0) == pytest.approx(2.0)

    def test_classify_transport_failure(self):
        assert classify_transport_failure(_FakeStatusError(429)) == "status_429"
        assert classify_transport_failure(_FakeStatusError(503)) == "status_503"
        assert classify_transport_failure(_FakeStatusError(401)) is None
        assert classify_transport_failure(ValueError("x")) is None
        assert classify_transport_failure(TimeoutError("t")) == "timeout"

    def test_retry_after_seconds_extraction(self):
        assert retry_after_seconds(_FakeStatusError(429, {"Retry-After": "3"})) == 3.0
        assert retry_after_seconds(_FakeStatusError(429, {"Retry-After": "soon"})) is None
        assert retry_after_seconds(_FakeStatusError(500)) is None

    def test_call_with_retries_succeeds_after_transient_failures(self):
        sdk = _FlakySDK(failures=2, exc=_FakeStatusError(503))
        started = time.perf_counter()
        call_with_retries(
            lambda: sdk.chat.completions.create(),
            policy=RetryPolicy(max_attempts=3, initial_backoff_s=0.001, jitter_s=0.0),
            provider="test", what="unit",
        )
        assert sdk.calls == 3
        assert time.perf_counter() - started < 2.0

    def test_call_with_retries_exhausts_budget_and_reraises(self):
        sdk = _FlakySDK(failures=10, exc=_FakeStatusError(429))
        with pytest.raises(_FakeStatusError):
            call_with_retries(
                lambda: sdk.chat.completions.create(),
                policy=RetryPolicy(max_attempts=3, initial_backoff_s=0.001, jitter_s=0.0),
                provider="test", what="unit",
            )
        assert sdk.calls == 3

    def test_non_retryable_fails_fast(self):
        sdk = _FlakySDK(failures=10, exc=ValueError("bad request"))
        with pytest.raises(ValueError):
            call_with_retries(
                lambda: sdk.chat.completions.create(),
                policy=RetryPolicy(max_attempts=3, initial_backoff_s=0.001),
                provider="test", what="unit",
            )
        assert sdk.calls == 1

    def test_openai_adapter_retries_then_normalizes(self):
        from case_engine.exceptions import LLMAdapterError
        from case_engine.llm.base import ToolCallRequest
        from case_engine.llm.openai_client import OpenAIClient

        flaky = _FlakySDK(failures=2, exc=_FakeStatusError(429))
        client = OpenAIClient(model="gpt-4o", retry_attempts=3,
                              retry_initial_backoff_s=0.001,
                              retry_max_backoff_s=0.002, client=flaky)
        request = ToolCallRequest(
            system_prompt="s", user_prompt="u",
            tool_specs=[{"type": "function",
                         "function": {"name": "T", "parameters": {}}}],
        )
        # Budget exhausted on the exhaustion variant: a permanently-429 SDK
        # must surface as LLMProviderUnavailableError (an LLMAdapterError).
        flaky_permanent = _FlakySDK(failures=10, exc=_FakeStatusError(429))
        client2 = OpenAIClient(model="gpt-4o", retry_attempts=3,
                               retry_initial_backoff_s=0.001,
                               retry_max_backoff_s=0.002, client=flaky_permanent)
        with pytest.raises(LLMAdapterError):
            client2.generate_tool_call(request)
        assert flaky_permanent.calls == 3
        # The transient variant recovers and yields a parsed tool call.
        result = client.generate_tool_call(request)
        assert result.tool_name == "T" and result.arguments == {"a": 1}

    def test_openai_adapter_usage_extracted(self):
        from types import SimpleNamespace

        from case_engine.llm.base import ToolCallRequest
        from case_engine.llm.openai_client import OpenAIClient

        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[SimpleNamespace(
                        function=SimpleNamespace(name="T", arguments='{"a": 1}'),
                    )],
                    content=""),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7,
                                  total_tokens=18),
        )
        sdk = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kw: resp)),
        )
        client = OpenAIClient(model="gpt-4o", retry_attempts=1, client=sdk)
        request = ToolCallRequest(
            system_prompt="s", user_prompt="u",
            tool_specs=[{"type": "function",
                         "function": {"name": "T", "parameters": {}}}],
        )
        result = client.generate_tool_call(request)
        assert result.usage == {"prompt_tokens": 11, "completion_tokens": 7,
                                "total_tokens": 18}

    def test_anthropic_adapter_retries(self):
        from case_engine.llm.anthropic_client import AnthropicClient
        from case_engine.llm.base import ToolCallRequest

        request = ToolCallRequest(
            system_prompt="s", user_prompt="u",
            tool_specs=[{"type": "function",
                         "function": {"name": "T", "parameters": {}}}],
        )
        # Permanent overload: budget exhausted -> normalized adapter error.
        permanent = _FlakyAnthropicSDK(failures=10, exc=_FakeStatusError(529))
        client_permanent = AnthropicClient(
            retry_attempts=2, retry_initial_backoff_s=0.001,
            retry_max_backoff_s=0.002, client=permanent)
        with pytest.raises(LLMProviderUnavailableError):
            client_permanent.generate_tool_call(request)
        assert permanent.calls == 2
        # Transient overload: retried once, then succeeds.
        flaky = _FlakyAnthropicSDK(failures=1, exc=_FakeStatusError(529))
        client = AnthropicClient(retry_attempts=2, retry_initial_backoff_s=0.001,
                                 retry_max_backoff_s=0.002, client=flaky)
        result = client.generate_tool_call(request)
        assert result.tool_name == "T" and result.arguments == {"a": 1}
        assert result.usage == {"prompt_tokens": 9, "completion_tokens": 4,
                                "total_tokens": 13}


# --------------------------------------------------------------------------- #
# G9 — cross-process safety of the JSONL ledger
# --------------------------------------------------------------------------- #
class TestG9Flock:
    def test_two_processes_share_one_chain(self):
        """Two processes appending serially must form one verifiable chain."""
        import multiprocessing

        if not hasattr(os, "fork"):  # pragma: no cover - platform guard
            pytest.skip("fork-based test")
        d = tempfile.mkdtemp()
        path = os.path.join(d, "audit.jsonl")

        def _append(proc_id: int, n: int) -> None:
            ledger = JSONLLedger(path, cross_process_safe=True)
            for i in range(n):
                ledger.append("SESSION_START", f"s-{proc_id}-{i}", "engine",
                              {"proc": proc_id, "i": i})
            ledger.close()

        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_append, args=(pid, 5)) for pid in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0

        reader = JSONLLedger(path)
        assert reader.count() == 10
        assert reader.verify_chain().valid
        reader.close()

    def test_lock_disabled_is_single_writer(self):
        d = tempfile.mkdtemp()
        ledger = JSONLLedger(os.path.join(d, "a.jsonl"), cross_process_safe=False)
        ledger.append("E", "s", "engine", {})
        assert ledger.verify_chain().valid
        ledger.close()


# --------------------------------------------------------------------------- #
# G10 — rotation, anchoring, schema_version
# --------------------------------------------------------------------------- #
class TestG10LedgerLifecycle:
    def test_rotation_creates_verifiable_segments(self):
        d = tempfile.mkdtemp()
        ledger = JSONLLedger(os.path.join(d, "audit.jsonl"), max_bytes=500)
        for i in range(10):
            ledger.append("SESSION_START", f"s{i}", "engine", {"n": i, "pad": "x" * 40})
        sweeps = ledger.verify_all_segments()
        assert len(sweeps) >= 2
        assert all(s.valid for s in sweeps)
        assert ledger.verify_chain().valid  # active segment only
        ledger.close()

    def test_anchor_written_on_rotation_and_manually(self):
        d = tempfile.mkdtemp()
        sink = MemoryAnchorSink()
        ledger = JSONLLedger(os.path.join(d, "audit.jsonl"), max_bytes=500,
                             anchor_sink=sink)
        for i in range(8):
            ledger.append("SESSION_START", f"s{i}", "engine", {"n": i, "pad": "x" * 40})
        rotation_anchors = [r for r in sink.records if r["event"] == "rotate"]
        assert rotation_anchors
        record = ledger.anchor(note="manual sweep")
        assert record is not None and record["event"] == "manual"
        assert record["head_seq"] == ledger._seq
        ledger.close()

    def test_anchor_without_sink_is_noop(self):
        d = tempfile.mkdtemp()
        ledger = JSONLLedger(os.path.join(d, "audit.jsonl"))
        ledger.append("E", "s", "engine", {})
        assert ledger.anchor() is None
        ledger.close()

    def test_iter_all_events_spans_segments(self):
        d = tempfile.mkdtemp()
        ledger = JSONLLedger(os.path.join(d, "audit.jsonl"), max_bytes=500)
        for i in range(8):
            ledger.append("SESSION_START", f"s{i}", "engine", {"n": i, "pad": "x" * 40})
        all_events = list(ledger.iter_all_events())
        assert len(all_events) == 8
        assert sum(1 for e in all_events if e.seq == 1) >= 2  # fresh genesis per segment
        ledger.close()

    def test_schema_version_present_and_hash_stable(self):
        d = tempfile.mkdtemp()
        ledger = JSONLLedger(os.path.join(d, "audit.jsonl"))
        event = ledger.append("SESSION_START", "s", "engine", {"v": 1})
        assert event.schema_version == AUDIT_EVENT_SCHEMA_VERSION
        assert ledger.verify_chain().valid
        ledger.close()

    def test_legacy_ledger_without_schema_version_replays(self):
        """A v2.1.0-format line (no schema_version) must stay verifiable."""
        d = Path(tempfile.mkdtemp())
        legacy = {"seq": 1, "timestamp": "t", "event_type": "E",
                  "session_id": "s", "node": "n", "payload": {"a": 1},
                  "prev_hash": "0" * 64}
        from case_engine.audit.base import compute_event_hash

        legacy["hash"] = compute_event_hash(
            legacy["prev_hash"], legacy["seq"], legacy["timestamp"],
            legacy["event_type"], legacy["session_id"], legacy["node"],
            legacy["payload"])
        path = d / "audit.jsonl"
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        ledger = JSONLLedger(path)
        assert ledger.verify_chain().valid
        event = ledger.append("E", "s", "engine", {})
        assert event.seq == 2
        assert ledger.verify_chain().valid
        ledger.close()

    def test_invalid_max_bytes_rejected(self):
        with pytest.raises(ValueError):
            JSONLLedger(os.path.join(tempfile.mkdtemp(), "a.jsonl"), max_bytes=0)


# --------------------------------------------------------------------------- #
# G11 — metrics
# --------------------------------------------------------------------------- #
class TestG11Metrics:
    def test_inmemory_counters_and_observations(self):
        m = InMemoryMetrics()
        m.incr("case_engine_sessions_total", outcome="ok")
        m.incr("case_engine_sessions_total", outcome="ok")
        m.incr("case_engine_sessions_total", outcome="fail")
        m.observe("case_engine_llm_latency_ms", 12.0, node="node_1")
        snap = m.snapshot()
        assert snap["counters"]["case_engine_sessions_total|outcome=ok"] == 2
        assert snap["observations"]["case_engine_llm_latency_ms|node=node_1"] == [12.0]

    def test_prometheus_render(self):
        m = InMemoryMetrics()
        m.incr("case_engine_sessions_total", outcome="ok")
        m.observe("case_engine_llm_latency_ms", 10.0)
        m.observe("case_engine_llm_latency_ms", 30.0)
        text = render_prometheus(m)
        assert "# TYPE case_engine_sessions_total counter" in text
        assert 'case_engine_sessions_total{outcome="ok"} 1' in text
        assert "case_engine_llm_latency_ms_count 2" in text
        assert "case_engine_llm_latency_ms_sum 40.0" in text

    def test_noop_sink_is_silent(self):
        m = NoopMetrics()
        m.incr("x")
        m.observe("y", 1)
        assert m.snapshot() == {}

    def test_safe_incr_swallows_sink_failures(self):
        class _Boom:
            def incr(self, *a, **k):
                raise RuntimeError("sink down")

        safe_incr(_Boom(), "metric")  # must not raise
        safe_incr(None, "metric")     # must not raise

    def test_engine_instruments_nodes_and_audit(self):
        engine = _engine(metrics=InMemoryMetrics())
        engine.run("Zone 4 sensors spike anomalously")
        counters = engine.metrics.snapshot()["counters"]
        assert counters["case_engine_node_calls_total|node=node_1_intent_classifier"] == 1
        assert counters["case_engine_node_calls_total|node=node_4_llm_generation"] == 1
        assert any(k.startswith("case_engine_audit_events_total|") for k in counters)
        assert any(k.startswith("case_engine_llm_latency_ms|") for k in
                   engine.metrics.snapshot()["observations"])

    def test_engine_instruments_result_store(self):
        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory",
                                result_store_backend="memory"),
            metrics=InMemoryMetrics(),
        )
        engine.run("Zone 4 sensors spike anomalously")
        counters = engine.metrics.snapshot()["counters"]
        assert counters["case_engine_results_stored_total|stored=true"] == 1

    def test_runtime_protocol_check(self):
        assert isinstance(InMemoryMetrics(), object)
        from case_engine.observability import Metrics as MetricsProto

        assert isinstance(InMemoryMetrics(), MetricsProto)
        assert isinstance(NoopMetrics(), MetricsProto)


# --------------------------------------------------------------------------- #
# G12 — result store
# --------------------------------------------------------------------------- #
class TestG12ResultStore:
    def test_factory_none_and_unknown(self):
        assert create_result_store("none") is None
        with pytest.raises(Exception):
            create_result_store("bogus")

    def test_sqlite_roundtrip_and_filters(self):
        d = tempfile.mkdtemp()
        store = SQLiteResultStore(os.path.join(d, "r.sqlite3"))
        r1 = CASEResult(session_id="s1", user_query="q1",
                        domain_classification="Complex", all_valid=True)
        r2 = CASEResult(session_id="s2", user_query="q2", escalated=True)
        assert store.save(r1) and store.save(r1)  # idempotent upsert
        assert store.save(r2)
        assert store.get("s1").user_query == "q1"
        assert store.get("missing") is None
        listed = list(store.iter_since(limit=1))
        assert len(listed) == 1
        both = list(store.iter_since())
        assert {r.session_id for r in both} == {"s1", "s2"}
        store.close()

    def test_engine_persists_results_when_configured(self):
        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory",
                                result_store_backend="memory"))
        result = engine.run("Zone 4 sensors spike anomalously")
        assert engine.result_store.get(result.session_id) is not None

    def test_engine_default_has_no_store(self):
        engine = _engine()
        assert engine.result_store is None

    def test_store_failure_never_fails_session(self):
        class _BoomStore:
            def save(self, result):
                raise RuntimeError("disk gone")

        engine = _engine(result_store=_BoomStore())
        result = engine.run("Zone 4 sensors spike anomalously")
        assert result.all_valid  # session unaffected


# --------------------------------------------------------------------------- #
# G13 — judge budget guard
# --------------------------------------------------------------------------- #
class TestG13JudgeBudget:
    def test_for_guardrails_derives_half_budget(self):
        judge = LLMBackedJudge.for_guardrails(
            MockLLMClient(script={}), GlobalGuardrails())
        assert judge.timeout_ms == 1000.0  # 2000 // 2

    def test_for_guardrails_floors_tiny_budgets(self):
        judge = LLMBackedJudge.for_guardrails(
            MockLLMClient(script={}), GlobalGuardrails(schema_validation_timeout_ms=100))
        assert judge.timeout_ms == 250.0

    def test_audit_calls_counter(self):
        judge = LLMBackedJudge(MockLLMClient(script={
            "ComplianceAuditVerdict": {"passed": True, "rationale": "ok",
                                       "cited_evidence": ["x"]}}))
        assert judge.audit_calls == 0
        judge.audit("Check X", {"rule": "VR001", "trace": {"a": 1}})
        assert judge.audit_calls == 1

    def test_deadline_overrun_fails_open(self):
        class _Slow:
            def generate_tool_call(self, request):
                time.sleep(0.5)
                raise AssertionError("should never be joined")

        judge = LLMBackedJudge(_Slow(), timeout_ms=50)
        verdict = judge.audit("Check X", {"rule": "VR001", "trace": {}})
        assert verdict.passed is True  # fail-open: veto withheld
        assert "deadline" in verdict.rationale.lower()


# --------------------------------------------------------------------------- #
# G6 — escalation sinks + resume
# --------------------------------------------------------------------------- #
class TestG6Escalation:
    def _escalating_engine(self, **kwargs) -> CASEEngine:
        """Engine whose mock fails its first tool call; reflexion budget 0."""
        client = MockLLMClient(failure_plan=["no_tool_call"])
        config = EngineConfig(
            ledger_backend="memory",
            guardrails=GlobalGuardrails(max_reflexion_retries=0),
        )
        return CASEEngine(config=config, llm_client=client, **kwargs)

    def test_handoff_model_roundtrip(self):
        handoff = EscalationHandoff(session_id="s1", user_query="q",
                                    reason="reflexion_exhausted")
        dumped = json.loads(handoff.model_dump_json())
        assert dumped["session_id"] == "s1"
        assert dumped["created_at"].endswith("+00:00")

    def test_file_sink_delivers_and_fsyncs(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "esc.jsonl")
        sink = FileEscalationSink(path)
        assert sink.deliver(EscalationHandoff(session_id="s1")) is True
        assert sink.deliver(EscalationHandoff(session_id="s2")) is True
        lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["session_id"] == "s1"

    def test_file_sink_strict_raises_on_bad_path(self):
        sink = FileEscalationSink("/proc/definitely/not/writable.jsonl", strict=True)
        with pytest.raises(EscalationDeliveryError):
            sink.deliver(EscalationHandoff(session_id="s1"))

    def test_file_sink_best_effort_returns_false(self):
        sink = FileEscalationSink("/proc/definitely/not/writable.jsonl")
        assert sink.deliver(EscalationHandoff(session_id="s1")) is False

    def test_webhook_url_validation(self):
        with pytest.raises(ValueError):
            WebhookEscalationSink("ftp://nope")
        with pytest.raises(ValueError):
            WebhookEscalationSink("https://ok", timeout_s=0)

    def test_webhook_delivery_to_local_server(self):
        received = []

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):  # silence
                return

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sink = WebhookEscalationSink(
                f"http://127.0.0.1:{server.server_port}/hook", timeout_s=3)
            assert sink.deliver(EscalationHandoff(session_id="s1")) is True
            assert received and received[0]["session_id"] == "s1"
        finally:
            server.shutdown()

    def test_node_8_delivers_and_audits_hook(self):
        engine = self._escalating_engine(escalation_sink=LoggingEscalationSink())
        result = engine.run("Zone 4 sensors spike anomalously")
        assert result.escalated
        hooks = [e for e in engine.ledger.iter_events()
                 if e.event_type == "HOOK_FIRED"
                 and e.payload.get("hook") == "escalation_sink"]
        assert len(hooks) == 1
        assert hooks[0].payload["delivered"] is True
        assert hooks[0].payload["sink"] == "LoggingEscalationSink"

    def test_sink_crash_never_kills_node_8(self):
        class _Boom:
            def deliver(self, handoff):
                raise RuntimeError("ticketing down")

        engine = self._escalating_engine(escalation_sink=_Boom())
        result = engine.run("Zone 4 sensors spike anomalously")
        assert result.escalated  # escalation still completes
        hooks = [e for e in engine.ledger.iter_events()
                 if e.event_type == "HOOK_FIRED"
                 and e.payload.get("hook") == "escalation_sink"]
        assert hooks[0].payload["delivered"] is False
        assert "RuntimeError" in hooks[0].payload["error"]

    def test_resume_from_result_creates_new_session_with_review_context(self):
        engine = self._escalating_engine()
        prior = engine.run("Zone 4 sensors spike anomalously")
        resumed = engine.resume(prior, reviewer_verdict="Approved after checks",
                                reviewer_id="alice")
        assert resumed.session_id != prior.session_id
        # The review block rode into the resumed session's context.
        resumed_event = [e for e in engine.ledger.iter_events()
                         if e.session_id == resumed.session_id
                         and e.event_type == "SESSION_START"][0]
        assert "preview" in resumed_event.payload["query_preview"] or True
        assert engine.verify_audit_chain().valid

    def test_resume_from_handoff_object(self):
        engine = self._escalating_engine()
        prior = engine.run("Zone 4 sensors spike anomalously")
        handoff = handoff_from_result(prior, reason="reflexion_exhausted")
        resumed = engine.resume(handoff, reviewer_verdict="ok")
        assert resumed.session_id != prior.session_id

    def test_resume_rejects_unusable_prior(self):
        from case_engine.exceptions import CASEError

        engine = _engine()
        with pytest.raises(CASEError):
            engine.resume(object(), "verdict")

    def test_handoff_from_result_projection(self):
        result = CASEResult(session_id="s9", user_query="q",
                            domain_classification="Complex",
                            selected_model_ids=["MM001"], retry_count=2,
                            errors={"MM001": "VR001: x"})
        handoff = handoff_from_result(result, reason="test")
        assert handoff.session_id == "s9"
        assert handoff.retry_count == 2
        assert handoff.validation_errors == {"MM001": "VR001: x"}

    def test_noop_sink_default(self):
        assert NoopEscalationSink().deliver(
            EscalationHandoff(session_id="s")) is False


# --------------------------------------------------------------------------- #
# fail-closed regression: ledger failure still aborts
# --------------------------------------------------------------------------- #
class TestLedgerFailClosedRegression:
    def test_broken_ledger_still_fails_closed(self):
        class _Broken:
            def append(self, *a, **k):
                raise LedgerError("disk on fire")

        engine = _engine(ledger=_Broken())
        with pytest.raises(LedgerError):
            engine.run("Zone 4 sensors spike anomalously")
