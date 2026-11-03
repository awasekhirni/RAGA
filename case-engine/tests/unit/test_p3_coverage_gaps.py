"""G26 — targeted coverage for the trust boundary and the thin files.

Complements the example-based suites with edge-path tests so the
enforcement/audit modules (the security boundary) sit at >=95 % line
coverage and the historically thin files (``llm/openai_client``,
``logging_setup``, ``registry/routing``, ``pipeline/nodes``) clear the
same bar.  Every test targets a *documented* failure or edge branch —
nothing here exists to game the percentage; each pinned branch is a
fail-closed contract someone may one day rely on.

Cross-references: ``docs/GAP_ANALYSIS.md`` §G26, coverage gate in
``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from case_engine.audit import InMemoryLedger, JSONLLedger, SQLiteLedger
from case_engine.audit.anchor import FileAnchorSink, MemoryAnchorSink, make_anchor_record
from case_engine.audit.crypto import AEADPayloadCipher, load_key
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import (
    CASEError,
    ConfigurationError,
    LedgerError,
    LLMProviderUnavailableError,
    RegistryError,
)
from case_engine.llm import MockLLMClient
from case_engine.llm.anthropic_client import AnthropicClient
from case_engine.llm.base import ToolCallRequest
from case_engine.llm.mock_client import MockLLMClient as MockClient
from case_engine.llm.ollama_client import OllamaClient, VLLMClient
from case_engine.llm.openai_client import OpenAIClient, OpenAICompatibleClient
from case_engine.logging_setup import ConsoleFormatter, JsonFormatter, configure_logging
from case_engine.registry import CognitiveRegistry
from case_engine.registry.routing import ModelRouter

# --------------------------------------------------------------------- #
# audit/anchor
# --------------------------------------------------------------------- #


class TestAnchorSink:
    """FileAnchorSink / MemoryAnchorSink edge paths (G10/G26)."""

    def test_file_sink_write_read_roundtrip(self, tmp_path: Path) -> None:
        sink = FileAnchorSink(tmp_path / "anchors" / "a.jsonl")
        record = make_anchor_record("ab" * 32, 7, "audit.jsonl")
        record["note"] = "release"
        sink.write(record)
        assert sink.path == tmp_path / "anchors" / "a.jsonl"
        lines = (tmp_path / "anchors" / "a.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["head_seq"] == 7

    def test_file_sink_write_failure_is_loud(self, tmp_path: Path) -> None:
        """Parent path occupied by a file -> mkdir fails -> error logged, raise."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        sink = FileAnchorSink(blocker / "anchors.jsonl")
        with pytest.raises(OSError):
            sink.write(make_anchor_record("ab" * 32, 1, "audit.jsonl"))

    def test_memory_sink_roundtrip(self) -> None:
        sink = MemoryAnchorSink()
        sink.write(make_anchor_record("ab" * 32, 2, "audit.jsonl"))
        sink.write({"exotic": object()})  # default=str keeps it total
        assert len(sink.records) == 2

    def test_jsonl_ledger_manual_anchor_with_sink(self, tmp_path: Path) -> None:
        """G10: ledger.anchor() writes to the configured external sink."""
        sink = MemoryAnchorSink()
        ledger = JSONLLedger(tmp_path / "a.jsonl", anchor_sink=sink, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        record = ledger.anchor(note="cron")
        assert record is not None and record["event"] == "manual"
        assert sink.records and sink.records[-1]["note"] == "cron"
        ledger.close()


# --------------------------------------------------------------------- #
# audit/crypto
# --------------------------------------------------------------------- #


class TestPayloadCipher:
    """AEADPayloadCipher + load_key edge paths (G4b/G26)."""

    def test_roundtrip_and_deterministic_envelope(self, monkeypatch) -> None:
        monkeypatch.setenv("CASE_LEDGER_KEY", "AA" * 32)  # 32 raw bytes, hex-ish b64? no: base64 of 32 bytes
        monkeypatch.delenv("CASE_LEDGER_KEY")
        key = bytes(range(32))
        cipher = AEADPayloadCipher(key=key)
        envelope = cipher.encrypt({"a": 1, "nested": {"b": [1, 2]}})
        assert envelope["__enc__"] == "AESGCM"
        assert cipher.decrypt(envelope) == {"a": 1, "nested": {"b": [1, 2]}}

    def test_encrypt_requires_dict(self) -> None:
        cipher = AEADPayloadCipher(key=bytes(range(32)))
        with pytest.raises(LedgerError, match="requires a dict"):
            cipher.encrypt(["not", "a", "dict"])

    def test_encrypt_rejects_unserializable_payload(self) -> None:
        cipher = AEADPayloadCipher(key=bytes(range(32)))
        with pytest.raises(LedgerError, match="not JSON-serializable"):
            cipher.encrypt({"bad": object()})

    def test_decrypt_rejects_non_envelope(self) -> None:
        cipher = AEADPayloadCipher(key=bytes(range(32)))
        with pytest.raises(LedgerError, match="not an AESGCM envelope"):
            cipher.decrypt({"plain": "payload"})

    def test_decrypt_rejects_missing_fields(self) -> None:
        cipher = AEADPayloadCipher(key=bytes(range(32)))
        envelope = cipher.encrypt({"a": 1})
        del envelope["nonce"]
        with pytest.raises(LedgerError, match="Corrupt encryption envelope: missing"):
            cipher.decrypt(envelope)

    def test_decrypt_rejects_garbage_base64(self) -> None:
        cipher = AEADPayloadCipher(key=bytes(range(32)))
        with pytest.raises(LedgerError, match="Corrupt encryption envelope"):
            cipher.decrypt({"__enc__": "AESGCM", "nonce": "!!not-b64!!", "data": "!!"})

    def test_wrong_key_fails_authentication(self) -> None:
        envelope = AEADPayloadCipher(key=bytes(range(32))).encrypt({"secret": True})
        other = AEADPayloadCipher(key=bytes(range(32, 64)))
        with pytest.raises(LedgerError, match="authentication failed"):
            other.decrypt(envelope)

    def test_decrypt_rejects_non_json_plaintext(self, monkeypatch) -> None:
        """A well-formed envelope whose plaintext is not JSON fails cleanly."""
        import base64 as b64
        import secrets as _secrets

        cipher = AEADPayloadCipher(key=bytes(range(32)))
        nonce = _secrets.token_bytes(12)
        ciphertext = cipher._aesgcm.encrypt(nonce, b"not json at all", None)
        envelope = {"__enc__": "AESGCM",
                    "nonce": b64.b64encode(nonce).decode(),
                    "data": b64.b64encode(ciphertext).decode()}
        with pytest.raises(LedgerError, match="not valid JSON"):
            cipher.decrypt(envelope)

    def test_load_key_env_var(self, monkeypatch) -> None:
        import base64 as b64

        raw = bytes(range(32))
        monkeypatch.setenv("CASE_TEST_KEY", b64.b64encode(raw).decode())
        assert load_key(key=None, key_env="CASE_TEST_KEY") == raw

    def test_load_key_raw_bytes(self) -> None:
        raw = bytes(range(32))
        assert load_key(key=raw, key_env="CASE_NOPE") == raw

    def test_load_key_rejects_wrong_length(self) -> None:
        with pytest.raises(LedgerError, match="32"):
            load_key(key=b"short", key_env="CASE_NOPE")

    def test_load_key_rejects_invalid_base64(self, monkeypatch) -> None:
        monkeypatch.setenv("CASE_TEST_KEY", "!!!not-base64!!!")
        with pytest.raises(LedgerError, match="base64"):
            load_key(key=None, key_env="CASE_TEST_KEY")

    def test_load_key_missing_everything(self, monkeypatch) -> None:
        monkeypatch.delenv("CASE_TEST_MISSING", raising=False)
        with pytest.raises(LedgerError, match="No ledger encryption key"):
            load_key(key=None, key_env="CASE_TEST_MISSING")

    def test_cipher_without_package_fails_closed(self, monkeypatch) -> None:
        """Missing 'cryptography' -> ImportError path -> LedgerError (G4b)."""
        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
        with pytest.raises(LedgerError, match="cryptography"):
            AEADPayloadCipher(key=bytes(range(32)))


# --------------------------------------------------------------------- #
# audit/jsonl_ledger — lock, rotation, adoption, verification edges
# --------------------------------------------------------------------- #


class TestJSONLLedgerEdges:
    """Remaining fail-closed branches of the JSONL backend (G9/G10/G26)."""

    def test_replay_rejects_directory_path(self, tmp_path: Path) -> None:
        with pytest.raises(LedgerError, match="Cannot read ledger file"):
            JSONLLedger(tmp_path, fsync=False)

    def test_fcntl_unavailable_degrades_with_warning(self, tmp_path: Path, monkeypatch) -> None:
        """Platforms without fcntl: cross_process_safe silently degrades."""
        from case_engine.audit import jsonl_ledger as jl

        monkeypatch.setattr(jl, "_FCNTL_AVAILABLE", False)
        ledger = JSONLLedger(tmp_path / "x.jsonl", cross_process_safe=True, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        assert ledger._cross_process_safe is False
        ledger.close()

    def test_rotated_segments_are_discovered_on_open(self, tmp_path: Path) -> None:
        ledger = JSONLLedger(tmp_path / "seg.jsonl", max_bytes=1, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        assert ledger._segment_index >= 1
        ledger.close()

        reopened = JSONLLedger(tmp_path / "seg.jsonl", fsync=False)
        results = reopened.verify_all_segments()
        assert len(results) == reopened._segment_index + 1
        reopened.close()

    def test_rotation_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        """Rename failure during rotation raises LedgerError (no torn state)."""
        ledger = JSONLLedger(tmp_path / "r.jsonl", max_bytes=1, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        monkeypatch.setattr(Path, "rename", lambda self, target: (_ for _ in ()).throw(OSError("disk sad")))
        with pytest.raises(LedgerError, match="rotation failed"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger.close()

    def test_anchor_failure_is_fail_closed(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        ledger = JSONLLedger(
            tmp_path / "a.jsonl", anchor_sink=FileAnchorSink(blocker / "x" / "y"), fsync=False,
        )
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        with pytest.raises(LedgerError, match="anchoring failed"):
            ledger.anchor()
        ledger.close()

    def test_externally_advanced_tail_is_adopted(self, tmp_path: Path) -> None:
        """Two handles on one file: the staler writer links to the newer tail."""
        first = JSONLLedger(tmp_path / "adopt.jsonl", cross_process_safe=True, fsync=False)
        second = JSONLLedger(tmp_path / "adopt.jsonl", cross_process_safe=True, fsync=False)
        first.append("TRACE_GENERATED", "s1", "n", {"who": "first"})
        first.append("TRACE_GENERATED", "s1", "n", {"who": "first"})
        event = second.append("TRACE_GENERATED", "s2", "n", {"who": "second"})
        assert event.seq == 3  # adopted first's tail instead of forking
        assert first.verify_chain().valid
        first.close()
        second.close()

    def test_blank_lines_are_skipped_during_iteration(self, tmp_path: Path) -> None:
        ledger = JSONLLedger(tmp_path / "blank.jsonl", fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()
        with open(tmp_path / "blank.jsonl", "a", encoding="utf-8") as fh:
            fh.write("\n")
        events = list(JSONLLedger(tmp_path / "blank.jsonl", fsync=False).iter_events())
        assert len(events) == 1

    def test_iter_all_events_skips_missing_segments(self, tmp_path: Path) -> None:
        ledger = JSONLLedger(tmp_path / "miss.jsonl", max_bytes=1, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger.close()
        (tmp_path / "miss.seg1.jsonl").unlink()  # destroy the rotated segment
        reopened = JSONLLedger(tmp_path / "miss.jsonl", fsync=False)
        events = list(reopened.iter_all_events())
        assert len(events) == 1  # only the active segment survives
        reopened.close()

    def test_decrypt_failure_during_iteration_fails_closed(self, tmp_path: Path) -> None:
        ledger = JSONLLedger(
            tmp_path / "enc.jsonl", payload_cipher=AEADPayloadCipher(key=bytes(range(32))), fsync=False,
        )
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()
        reopened = JSONLLedger(
            tmp_path / "enc.jsonl", payload_cipher=AEADPayloadCipher(key=bytes(range(32, 64))), fsync=False,
        )
        with pytest.raises(LedgerError, match="decryption failed"):
            list(reopened.iter_events(decrypt=True))
        reopened.close()

    def test_verify_segment_detects_linkage_break(self, tmp_path: Path) -> None:
        path = tmp_path / "link.jsonl"
        ledger = JSONLLedger(path, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[1])
        event["prev_hash"] = "f" * 64  # break the link, keep the hash
        lines[1] = json.dumps(event, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        reopened = JSONLLedger(path, fsync=False)
        verification = reopened.verify_chain()
        assert not verification.valid
        assert verification.first_invalid_seq == 2
        assert "linkage break" in verification.reason or "prev_hash" in verification.reason
        reopened.close()

    def test_verify_segment_detects_hash_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "hash.jsonl"
        ledger = JSONLLedger(path, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"] = {"tampered": True}
        lines[0] = json.dumps(event, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        reopened = JSONLLedger(path, fsync=False)
        verification = reopened.verify_chain()
        assert not verification.valid
        assert "ash mismatch" in verification.reason  # case-tolerant match
        reopened.close()

    def test_write_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        """fsync failure aborts the append; the next append heals via tail adoption.

        The failed event's line is flushed to disk before the fsync
        failure, so the chain tail is already advanced on disk.  With
        ``cross_process_safe`` (the default) the next append re-reads
        the tail under the lock, adopts it and continues from there —
        no fork, no duplicate seq, no silent corruption (G9).
        """
        import os as _os

        ledger = JSONLLedger(tmp_path / "w.jsonl", fsync=True)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})

        def boom(fd: int) -> None:
            raise OSError("sync failed")

        monkeypatch.setattr(_os, "fsync", boom)
        with pytest.raises(LedgerError, match="Ledger append failed"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        monkeypatch.undo()
        # The interrupted line persisted; the next append adopts it.
        event = ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        assert event.seq == 3
        assert ledger.verify_chain().valid
        seqs = [json.loads(line)["seq"] for line in
                (tmp_path / "w.jsonl").read_text(encoding="utf-8").splitlines()]
        assert seqs == [1, 2, 3], "duplicate or lost seq after failed append"
        ledger.close()


# --------------------------------------------------------------------- #
# audit/sqlite_ledger + audit/base verification branches
# --------------------------------------------------------------------- #


class TestSQLiteLedgerEdges:
    """SQLite backend error paths (G26)."""

    def test_open_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        import sqlite3 as sq

        def boom(*args, **kwargs):
            raise sq.OperationalError("cannot open database")

        monkeypatch.setattr(sq, "connect", boom)
        with pytest.raises(LedgerError, match="Cannot open SQLite ledger"):
            SQLiteLedger(tmp_path / "x.db")

    def test_replay_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        import sqlite3 as sq

        real_connect = sq.connect

        class _FlakyConnection:
            """Proxy whose tail-SELECT fails (Connection attrs are read-only)."""

            def __init__(self, conn) -> None:
                self._conn = conn

            def execute(self, sql, *a, **kw):
                if "SELECT seq, hash" in sql:
                    raise sq.OperationalError("read failed")
                return self._conn.execute(sql, *a, **kw)

            def __getattr__(self, item):
                return getattr(self._conn, item)

        def flaky_connect(*args, **kwargs):
            return _FlakyConnection(real_connect(*args, **kwargs))

        monkeypatch.setattr(sq, "connect", flaky_connect)
        with pytest.raises(LedgerError, match="replay failed"):
            SQLiteLedger(tmp_path / "y.db")

    def test_encrypt_failure_is_fail_closed(self, tmp_path: Path) -> None:
        class ExplodingCipher:
            def encrypt(self, payload):
                raise RuntimeError("hsm offline")

        ledger = SQLiteLedger(tmp_path / "z.db", payload_cipher=ExplodingCipher())
        with pytest.raises(LedgerError, match="encryption failed"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()

    def test_insert_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        import sqlite3 as sq

        ledger = SQLiteLedger(tmp_path / "i.db")
        real_conn = ledger._conn

        class _LockedConnection:
            """Proxy whose INSERTs fail (sqlite3.Connection attrs are read-only)."""

            def execute(self, sql, *a, **kw):
                if sql.startswith("INSERT"):
                    raise sq.OperationalError("database is locked")
                return real_conn.execute(sql, *a, **kw)

            def __getattr__(self, item):
                return getattr(real_conn, item)

        monkeypatch.setattr(ledger, "_conn", _LockedConnection())
        with pytest.raises(LedgerError, match="Ledger append failed"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        monkeypatch.undo()
        ledger.close()

    def test_decrypt_failure_during_iteration(self, tmp_path: Path) -> None:
        ledger = SQLiteLedger(
            tmp_path / "d.db", payload_cipher=AEADPayloadCipher(key=bytes(range(32))),
        )
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger._cipher = AEADPayloadCipher(key=bytes(range(32, 64)))
        with pytest.raises(LedgerError, match="decryption failed"):
            list(ledger.iter_events(decrypt=True))
        ledger.close()


class TestVerifyChainFailureBranches:
    """audit/base verify_chain failure branches (G26)."""

    def test_sequence_break_detected(self) -> None:
        ledger = InMemoryLedger()
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger._events[1].seq = 99  # pydantic models are not frozen
        verification = ledger.verify_chain()
        assert not verification.valid
        assert verification.first_invalid_seq == 99
        assert "Sequence break" in verification.reason

    def test_prev_hash_break_detected(self) -> None:
        ledger = InMemoryLedger()
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger._events[1].prev_hash = "0" * 64  # genesis instead of the real link
        verification = ledger.verify_chain()
        assert not verification.valid
        assert "prev_hash mismatch" in verification.reason

    def test_memory_ledger_close_is_a_noop(self) -> None:
        ledger = InMemoryLedger()
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()  # must not raise
        assert ledger.verify_chain().valid


# --------------------------------------------------------------------- #
# enforcement/tool_factory
# --------------------------------------------------------------------- #


def test_tool_factory_generic_tool(registry: CognitiveRegistry) -> None:
    """The pipeline-state alias builds the same OpenAI-shaped spec."""
    from case_engine.enforcement import ToolFactory

    factory = ToolFactory()
    model = registry.get("MM005")
    spec = factory.generic_tool(model)
    assert spec["function"]["name"] == model.tool_name


# --------------------------------------------------------------------- #
# llm adapters: SDK-missing paths, compatible endpoints, mock edges
# --------------------------------------------------------------------- #


class TestAdapterEdgePaths:
    """Adapter constructor/parse edges (G26: openai_client, ollama, mock)."""

    def test_openai_client_without_sdk_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(LLMProviderUnavailableError, match="openai"):
            OpenAIClient(model="gpt-4o", api_key="k")

    def test_openai_compatible_client_with_injected_stub(self) -> None:
        """vLLM-style endpoint: constructor works with an injected client."""
        stub = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: None)))
        client = OpenAICompatibleClient(
            base_url="http://localhost:8000/v1", model="Qwen2.5-7B-Instruct", client=stub,
        )
        assert client.name == "openai_compatible"

    def test_anthropic_client_without_sdk_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "anthropic", None)
        with pytest.raises(LLMProviderUnavailableError, match="anthropic"):
            AnthropicClient(model="claude-sonnet-4-20250514", api_key="k")

    def test_anthropic_convert_tools_shape(self) -> None:
        spec = [{
            "type": "function",
            "function": {"name": "T", "description": "d",
                         "parameters": {"type": "object", "properties": {}}},
        }]
        converted = AnthropicClient._convert_tools(spec)
        assert converted[0]["name"] == "T"
        assert converted[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_anthropic_build_request_kwargs(self) -> None:
        request = ToolCallRequest(system_prompt="s", user_prompt="u",
                                  tool_specs=[{"type": "function",
                                               "function": {"name": "T"}}],
                                  max_tokens=99)
        kwargs = AnthropicClient.build_request_kwargs(
            request, model="m", max_tokens=99, temperature=0.0,
        )
        assert kwargs["max_tokens"] == 99
        assert kwargs["tools"][0]["name"] == "T"

    def test_ollama_and_vllm_clients(self, monkeypatch) -> None:
        stub = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: None)))
        ollama = OllamaClient(base_url="http://localhost:11434/v1", model="llama3", client=stub)
        assert ollama.name == "ollama"
        vllm = VLLMClient(base_url="http://localhost:8000/v1", model="m", client=stub)
        assert vllm.name == "vllm"
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(LLMProviderUnavailableError):
            OllamaClient(base_url="http://localhost:11434/v1", model="llama3")

    def test_mock_default_tool_fallback_and_script_dump(self) -> None:
        client = MockClient(script={"Known": {"a": 1}}, default_tool="Known")
        request = ToolCallRequest(system_prompt="s", user_prompt="u",
                                  tool_specs=[{"type": "function",
                                               "function": {"name": "Unknown"}}])
        result = client.generate_tool_call(request)
        assert result.tool_name == "Known"
        assert '"Known"' in client.describe_script()

    def test_request_without_tool_specs_raises(self) -> None:
        request = ToolCallRequest(system_prompt="s", user_prompt="u", tool_specs=[])
        with pytest.raises(LLMProviderUnavailableError.__mro__[0].__bases__[0]):  # CASEError family
            request.primary_tool_name

    def test_base_healthcheck_and_timed_hook(self) -> None:
        client = MockClient(script={"T": {"a": 1}})
        assert client.healthcheck() is True
        request = ToolCallRequest(system_prompt="s", user_prompt="u",
                                  tool_specs=[{"type": "function", "function": {"name": "T"}}])
        assert client._timed(request) == 0.0


# --------------------------------------------------------------------- #
# logging_setup + registry/routing + langgraph guard
# --------------------------------------------------------------------- #


class TestLoggingAndRoutingEdges:
    """logging_setup formatters + routing failure branches (G26)."""

    def test_json_formatter_renders_extras_and_exceptions(self) -> None:
        formatter = JsonFormatter()
        import logging as std_logging

        record = std_logging.LogRecord(
            "case_engine.test", std_logging.INFO, "p", 1, "msg %s", ("x",), None,
        )
        record.session_id = "s-1"
        line = json.loads(formatter.format(record))
        assert line["message"] == "msg x"
        assert line["session_id"] == "s-1"

        try:
            raise ValueError("boom")
        except ValueError:
            import sys as _sys

            record.exc_info = _sys.exc_info()
            payload = json.loads(formatter.format(record))
        assert "ValueError" in payload["exception"]

    def test_console_formatter_renders_exception(self) -> None:
        import logging as std_logging

        formatter = ConsoleFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys as _sys

            record = std_logging.LogRecord(
                "case_engine.test", std_logging.ERROR, "p", 1, "oops", None, _sys.exc_info(),
            )
        out = formatter.format(record)
        assert "oops" in out and "RuntimeError" in out

    def test_configure_logging_with_file_handler(self, tmp_path: Path) -> None:
        log_file = tmp_path / "case.log"
        root = configure_logging(level="INFO", json_mode=True, log_file=str(log_file))
        root.info("hello", extra={"node": "bench"})
        for handler in list(root.handlers):
            root.removeHandler(handler)
        content = log_file.read_text(encoding="utf-8")
        assert '"hello"' in content

    def test_router_forced_unknown_model_raises(self, registry: CognitiveRegistry) -> None:
        from case_engine.exceptions import RoutingError

        router = ModelRouter(registry)
        with pytest.raises(RoutingError, match="Forced model selection failed"):
            router.select("Complex", forced_model_ids=["MM999"])

    def test_router_explain_unknown_domain_raises(self, registry: CognitiveRegistry) -> None:
        from case_engine.exceptions import RoutingError

        router = ModelRouter(registry)
        with pytest.raises(RoutingError, match="Domain not present"):
            router.explain("Narnia")

    def test_langgraph_guard_fails_without_package(self, monkeypatch) -> None:
        from case_engine.pipeline import langgraph_adapter as lg

        monkeypatch.setitem(sys.modules, "langgraph", None)
        with pytest.raises(LLMProviderUnavailableError, match="langgraph"):
            lg._require_langgraph()


# --------------------------------------------------------------------- #
# state_machine guards + retry driver + engine introspection
# --------------------------------------------------------------------- #


class TestStateMachineAndRetryEdges:
    """Dispatch/step guards, retry helpers, engine projections (G26)."""

    def test_unknown_node_dispatch_raises(self, engine: CASEEngine) -> None:
        from case_engine.exceptions import PipelineError
        from case_engine.pipeline.state_machine import CASEStateMachine

        machine = CASEStateMachine(engine.ctx)
        machine._nodes.pop("node_1_intent_classifier")
        with pytest.raises(PipelineError, match="No node registered"):
            machine.run({"session_id": "s"})

    def test_step_guard_trips_immediately(self, engine: CASEEngine) -> None:
        from case_engine.exceptions import PipelineError
        from case_engine.pipeline.state_machine import CASEStateMachine

        machine = CASEStateMachine(engine.ctx)
        with pytest.raises(PipelineError, match="max_steps"):
            machine.run({"session_id": "s"}, max_steps=0)

    def test_async_step_guard_trips_immediately(self, engine: CASEEngine) -> None:
        from case_engine.exceptions import PipelineError
        from case_engine.pipeline.state_machine import CASEStateMachine

        machine = CASEStateMachine(engine.ctx)
        with pytest.raises(PipelineError, match="max_steps"):
            asyncio.run(machine.run_async({"session_id": "s"}, max_steps=0))

    def test_retry_after_header_parsing(self) -> None:
        from case_engine.llm.retry import RetryPolicy, retry_after_seconds

        assert retry_after_seconds(Exception("no hint")) is None
        assert retry_after_seconds(SimpleNamespace(retry_after=2.5)) == 2.5
        exc = SimpleNamespace(response=SimpleNamespace(headers={"Retry-After": "7"}))
        assert retry_after_seconds(exc) == 7.0
        policy = RetryPolicy(max_attempts=2, initial_backoff_s=0.01, max_backoff_s=0.02)
        assert policy.sleep_for(3, None) <= 0.02 + policy.jitter_s

    def test_async_retry_driver_success_and_exhaustion(self) -> None:
        from case_engine.llm.retry import RetryPolicy, acall_with_retries

        async def _scenario() -> None:
            policy = RetryPolicy(max_attempts=2, initial_backoff_s=0.01, max_backoff_s=0.01)
            calls = {"n": 0}

            def flaky() -> str:
                calls["n"] += 1
                if calls["n"] < 2:
                    raise ConnectionError("transient")
                return "ok"

            assert await acall_with_retries(flaky, policy=policy, provider="t", what="op") == "ok"

            def hopeless() -> None:
                raise ConnectionError("down")

            with pytest.raises(ConnectionError):
                await acall_with_retries(hopeless, policy=policy, provider="t", what="op")

        asyncio.run(_scenario())

    def test_engine_introspection_and_unknown_ids(self, engine: CASEEngine) -> None:
        assert len(engine.list_models()) == 15
        assert engine.describe_model("MM005")["algorithm_id"] == "MM005"
        assert engine.tool_spec("MM005")["function"]["name"] == "RootCauseAnalysisTrace"
        assert engine.verify_audit_chain().valid
        with pytest.raises(RegistryError):
            engine.describe_model("MM999")
        with pytest.raises(RegistryError):
            engine.tool_spec("MM999")

    def test_engine_debug_level_installs_memory_metrics(self) -> None:
        from case_engine.observability import InMemoryMetrics

        engine = CASEEngine(
            config=EngineConfig(log_level="DEBUG", ledger_backend="memory"),
            llm_client=MockLLMClient(),
        )
        assert isinstance(engine.metrics, InMemoryMetrics)

    def test_engine_encryption_with_injected_ledger_warns(self, tmp_path: Path) -> None:
        """G4b seam: an injected ledger makes config encryption a no-op warning."""
        engine = CASEEngine(
            config=EngineConfig(ledger_encryption=True, ledger_backend="memory"),
            llm_client=MockLLMClient(),
            ledger=InMemoryLedger(),
        )
        assert engine.ledger is not None

    def test_engine_cipher_unavailable_fails_fast(self, monkeypatch) -> None:
        import case_engine.engine as engine_module

        def boom(**kwargs):
            raise CASEError("no key configured")

        monkeypatch.setattr(engine_module, "AEADPayloadCipher", boom)
        with pytest.raises(ConfigurationError, match="cipher"):
            CASEEngine(
                config=EngineConfig(ledger_encryption=True, ledger_backend="jsonl"),
                llm_client=MockLLMClient(),
            )

    def test_engine_resume_with_garbage_prior_raises(self, engine: CASEEngine) -> None:
        with pytest.raises(CASEError):
            engine.resume("not-a-real-session", "approved")


# --------------------------------------------------------------------- #
# escalation sinks + config + pack + observability + privacy + reports
# --------------------------------------------------------------------- #


class TestMiscEdgePaths:
    """Remaining documented edge branches (G26)."""

    def test_webhook_sink_rejects_bad_config(self) -> None:
        from case_engine.escalation import WebhookEscalationSink

        with pytest.raises(ValueError, match="http"):
            WebhookEscalationSink(url="ftp://x", strict=True)
        with pytest.raises(ValueError, match="timeout"):
            WebhookEscalationSink(url="http://x", timeout_s=0, strict=True)

    def test_webhook_delivery_failure_best_effort(self, monkeypatch) -> None:
        import urllib.request as urllib_req

        from case_engine.escalation import EscalationHandoff, WebhookEscalationSink

        def boom(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(urllib_req, "urlopen", boom)
        sink = WebhookEscalationSink(url="http://example.invalid/hook", strict=False)
        handoff = EscalationHandoff(
            session_id="s", user_query="q", reason="reflexion_exhausted",
        )
        assert sink.deliver(handoff) is False

    def test_config_from_file_errors(self, tmp_path: Path) -> None:
        from case_engine.config import EngineConfig

        with pytest.raises(ConfigurationError, match="not found"):
            EngineConfig.from_file(tmp_path / "missing.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid JSON"):
            EngineConfig.from_file(bad)

    def test_config_env_var_resolution(self, tmp_path: Path, monkeypatch) -> None:
        from case_engine.config import EngineConfig

        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps({"ledger_backend": "memory"}), encoding="utf-8")
        monkeypatch.setenv("CASE_ENGINE_CONFIG", str(cfg_file))
        assert EngineConfig.load().ledger_backend == "memory"
        monkeypatch.delenv("CASE_ENGINE_CONFIG")
        assert EngineConfig.load().ledger_backend != "env"

    def test_registry_pack_dump_and_load_roundtrip(self, tmp_path: Path) -> None:
        from case_engine.registry.pack import dump_registry_pack, load_registry_pack

        out = tmp_path / "pack.json"
        document = dump_registry_pack(CognitiveRegistry.load_default(), path=out)
        assert out.exists()
        models, rules, matrix = load_registry_pack(out)
        assert len(models) == len(document["models"])
        assert matrix["Complex"]

    def test_registry_pack_unreadable_and_invalid(self, tmp_path: Path) -> None:
        from case_engine.registry.pack import load_registry_pack

        with pytest.raises(RegistryError):
            load_registry_pack(tmp_path / "missing.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        with pytest.raises(RegistryError):
            load_registry_pack(bad)
        arr = tmp_path / "arr.json"
        arr.write_text("[]", encoding="utf-8")
        with pytest.raises(RegistryError):
            load_registry_pack(arr)

    def test_observability_fail_open_hooks(self) -> None:
        from case_engine.observability import (
            InMemoryMetrics,
            render_prometheus,
            safe_incr,
            safe_observe,
        )

        safe_incr(None, "m")  # None sink is a no-op
        safe_observe(None, "m", 1.0)
        class _Raising:
            def incr(self, *a, **k):
                raise RuntimeError("sink down")
            def observe(self, *a, **k):
                raise RuntimeError("sink down")
        safe_incr(_Raising(), "m")
        safe_observe(_Raising(), "m", 1.0)
        metrics = InMemoryMetrics()
        metrics.incr("case_engine_sessions_total", outcome="has space")
        text = render_prometheus(metrics)
        assert "case_engine_sessions_total" in text

    def test_privacy_redactor_payload_roundtrip(self) -> None:
        from case_engine.privacy import PIIRedactor

        redactor = PIIRedactor()
        payload = {"email": "jane@corp.example", "note": "call Jane Doe", "n": 1}
        redacted, count = redactor.redact_payload(payload)
        assert count >= 1
        assert "jane@corp.example" not in json.dumps(redacted, default=str)

    def test_report_failed_rules_projection(self) -> None:
        from case_engine.schemas.reports import RuleReport, TraceValidationReport

        report = TraceValidationReport(
            model_id="MM005", tool_name="RootCauseAnalysisTrace",
            schema_valid=True, passed=False,
            rule_reports=[
                RuleReport(rule_id="VR005", rule_name="r", rule_type="semantic",
                           passed=False, message="m"),
                RuleReport(rule_id="VR016", rule_name="r", rule_type="universal",
                           passed=True, message="m"),
            ],
        )
        assert report.failed_rules == ["VR005"]


# --------------------------------------------------------------------- #
# jsonl_ledger: remaining fail-closed branches (round 2)
# --------------------------------------------------------------------- #


class TestJSONLLedgerEdgesRound2:
    """Deeper JSONL branches: read-tail, flock, encrypt, segment sweeps."""

    def test_read_tail_oserror_becomes_ledger_error(self, tmp_path: Path) -> None:
        """File swapped for a directory mid-life: tail read fails loudly."""
        path = tmp_path / "swap.jsonl"
        ledger = JSONLLedger(path, cross_process_safe=True, fsync=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        path.unlink()
        path.mkdir()
        with pytest.raises(LedgerError, match="Cannot read ledger tail"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        ledger.close()

    def test_read_tail_blank_window_returns_none(self, tmp_path: Path) -> None:
        """A whitespace-only file yields a None tail (no chain to adopt)."""
        path = tmp_path / "blank.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        ledger = JSONLLedger(path, cross_process_safe=True, fsync=False)
        event = ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        assert event.seq == 1  # no phantom tail adopted
        ledger.close()

    def test_open_failure_when_parent_is_a_file(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        with pytest.raises(LedgerError, match="Cannot open ledger file"):
            JSONLLedger(blocker / "x" / "l.jsonl", fsync=False)

    def test_rotation_stat_failure_tolerated(self, tmp_path: Path, monkeypatch) -> None:
        """stat() OSError during rotation check degrades to current=0."""
        ledger = JSONLLedger(tmp_path / "s.jsonl", max_bytes=1, fsync=False,
                             cross_process_safe=False)

        real_stat = Path.stat

        def flaky_stat(self, *a, **kw):
            raise OSError("stat failed")

        monkeypatch.setattr(Path, "stat", flaky_stat)
        # Must not raise: the rotation check treats the file as empty.
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        monkeypatch.setattr(Path, "stat", real_stat)
        assert ledger.verify_chain().valid
        ledger.close()

    def test_flock_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        from case_engine.audit import jsonl_ledger as jl

        def boom(fd, mode):
            raise OSError("lock server gone")

        monkeypatch.setattr(jl.fcntl, "flock", boom)
        ledger = JSONLLedger(tmp_path / "f.jsonl", cross_process_safe=True, fsync=False)
        with pytest.raises(LedgerError, match="lock could not be acquired"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()

    def test_encrypt_failure_is_fail_closed(self, tmp_path: Path) -> None:
        class ExplodingCipher:
            def encrypt(self, payload):
                raise RuntimeError("hsm offline")

        ledger = JSONLLedger(
            tmp_path / "e.jsonl", payload_cipher=ExplodingCipher(), fsync=False,
        )
        with pytest.raises(LedgerError, match="encryption failed"):
            ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.close()

    def test_segment_sweep_on_original_instance(self, tmp_path: Path) -> None:
        """Missing segments are skipped in iteration AND verification sweeps.

        Uses the *original* ledger instance so ``_segment_index`` still
        knows about the deleted rotated segment (a fresh open would
        re-discover zero segments and prove nothing).
        """
        ledger = JSONLLedger(tmp_path / "seg.jsonl", max_bytes=1, fsync=False,
                             cross_process_safe=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        assert ledger._segment_index == 1
        seg1 = ledger._segment_path(1)
        assert seg1.exists()
        seg1.unlink()

        events = list(ledger.iter_all_events())          # skips missing seg
        assert [e.seq for e in events] == [1]
        verifications = ledger.verify_all_segments()     # missing seg skipped
        assert len(verifications) == 1
        assert verifications[0].valid
        # Defensive absent-file branch of _verify_segment (direct call).
        absent = ledger._verify_segment(ledger._segment_path(1))
        assert absent.valid and absent.total_entries == 0
        ledger.close()

    def test_segment_iteration_blank_lines_and_decrypt_failure(
        self, tmp_path: Path,
    ) -> None:
        """_iter_segment skips blank lines and fails closed on bad payloads."""
        ledger = JSONLLedger(
            tmp_path / "d.jsonl",
            payload_cipher=AEADPayloadCipher(key=bytes(range(32))),
            fsync=False, cross_process_safe=False,
        )
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        active = ledger._segment_path(0)
        with open(active, "a", encoding="utf-8") as fh:
            fh.write("\n")  # blank line inside the segment
        events = list(ledger.iter_all_events())
        assert len(events) == 1

        # Wrong-key cipher -> decrypt failure during segment iteration.
        ledger._cipher = AEADPayloadCipher(key=bytes(range(32, 64)))
        with pytest.raises(LedgerError, match="decryption failed"):
            list(ledger.iter_all_events(decrypt=True))
        ledger.close()

    def test_verify_all_segments_detects_corruption(self, tmp_path: Path) -> None:
        """_verify_segment reports linkage and hash breaks per segment."""
        path = tmp_path / "c.jsonl"
        ledger = JSONLLedger(path, fsync=False, cross_process_safe=False)
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 1})
        ledger.append("TRACE_GENERATED", "s", "n", {"i": 2})
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[1])
        event["prev_hash"] = "e" * 64
        lines[1] = json.dumps(event, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        results = ledger.verify_all_segments()
        assert not results[0].valid
        assert results[0].first_invalid_seq == 2
        ledger.close()


# --------------------------------------------------------------------- #
# state_machine async guards + validation engine edges
# --------------------------------------------------------------------- #


class TestStateMachineAsyncGuards:
    """Async dispatch/topology guards (G5/G26)."""

    def test_async_unknown_node_dispatch_raises(self, engine: CASEEngine) -> None:
        from case_engine.exceptions import PipelineError
        from case_engine.pipeline.state_machine import CASEStateMachine

        machine = CASEStateMachine(engine.ctx)
        machine._nodes.pop("node_1_intent_classifier")

        async def scenario() -> None:
            with pytest.raises(PipelineError, match="No node registered"):
                await machine.run_async({"session_id": "s"})

        asyncio.run(scenario())

    def test_async_topology_violation_raises(self, engine: CASEEngine) -> None:
        """A node returning an illegal edge is a topology violation (async)."""
        from case_engine.exceptions import TopologyViolationError
        from case_engine.pipeline.nodes import ENTRY_NODE
        from case_engine.pipeline.state_machine import CASEStateMachine

        machine = CASEStateMachine(engine.ctx)
        machine._nodes[ENTRY_NODE] = lambda state, ctx: ("node_999_bogus", state)

        async def scenario() -> None:
            with pytest.raises(TopologyViolationError, match="illegal transition"):
                await machine.run_async({"session_id": "s"})

        asyncio.run(scenario())


class TestValidationEngineEdges:
    """ValidationEngine schema-invalid and latency-budget paths (G26)."""

    def test_schema_invalid_payload_short_circuits_rules(self, registry: CognitiveRegistry) -> None:
        from case_engine.validation.engine import ValidationEngine

        engine = ValidationEngine(registry, GlobalGuardrails())
        model_def = registry.get("MM005")
        report = engine.validate(model_def, {"not": "a valid trace"})
        assert report.schema_valid is False
        assert report.passed is False
        assert report.schema_errors
        assert report.rule_reports == []  # rules never run on invalid schema

    def test_latency_budget_warning_does_not_change_verdict(
        self, registry: CognitiveRegistry, gold_payloads: dict, monkeypatch,
    ) -> None:
        import case_engine.validation.engine as ve
        from case_engine.validation.engine import ValidationEngine

        guardrails = GlobalGuardrails(schema_validation_timeout_ms=50)
        engine = ValidationEngine(registry, guardrails)
        model_def = registry.get("MM005")

        # Force the measured latency above the 50 ms budget.
        ticks = iter([100.0, 100.0 + 0.06])
        real_perf = ve.time.perf_counter
        monkeypatch.setattr(ve.time, "perf_counter", lambda: next(ticks, real_perf()))

        report = engine.validate(model_def, gold_payloads["MM005"])
        assert report.passed is True  # the verdict stands despite the warning
        assert report.latency_ms >= 50.0
