"""Unit tests: hash-chained ledger backends + tamper evidence."""

from __future__ import annotations

import json

import pytest

from case_engine.audit import (
    GENESIS_HASH,
    InMemoryLedger,
    JSONLLedger,
    SQLiteLedger,
    canonical_json,
    create_ledger,
)
from case_engine.audit.base import compute_event_hash
from case_engine.config import EngineConfig
from case_engine.engine import CASEEngine
from case_engine.exceptions import ConfigurationError, LedgerError
from case_engine.llm import MockLLMClient


def _append_three(ledger) -> None:
    for i in range(3):
        ledger.append("SESSION_START", "s-1", "engine", {"i": i})


class TestChainMath:
    def test_canonical_json_is_key_order_stable(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_genesis_hash_shape(self) -> None:
        assert len(GENESIS_HASH) == 64 and set(GENESIS_HASH) == {"0"}

    def test_hash_changes_with_payload(self) -> None:
        h1 = compute_event_hash(GENESIS_HASH, 1, "t", "E", "s", "n", {"a": 1})
        h2 = compute_event_hash(GENESIS_HASH, 1, "t", "E", "s", "n", {"a": 2})
        assert h1 != h2


class TestMemoryLedger:
    def test_chain_verifies(self) -> None:
        ledger = InMemoryLedger()
        _append_three(ledger)
        v = ledger.verify_chain()
        assert v.valid and v.total_entries == 3

    def test_first_entry_links_genesis(self) -> None:
        ledger = InMemoryLedger()
        event = ledger.append("E", "s", "n", {})
        assert event.prev_hash == GENESIS_HASH and event.seq == 1


class TestJSONLLedger:
    def test_append_and_verify(self, tmp_path) -> None:
        path = tmp_path / "audit.jsonl"
        ledger = JSONLLedger(path)
        _append_three(ledger)
        ledger.close()
        assert ledger.verify_chain().valid
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["prev_hash"] == GENESIS_HASH

    def test_reopen_continues_chain(self, tmp_path) -> None:
        path = tmp_path / "audit.jsonl"
        first = JSONLLedger(path)
        _append_three(first)
        first.close()
        second = JSONLLedger(path)
        event = second.append("SESSION_COMPLETED", "s-1", "engine", {})
        assert event.seq == 4
        assert second.verify_chain().valid
        second.close()

    def test_tampering_detected(self, tmp_path) -> None:
        path = tmp_path / "audit.jsonl"
        ledger = JSONLLedger(path)
        _append_three(ledger)
        ledger.close()

        lines = path.read_text().strip().splitlines()
        record = json.loads(lines[1])
        record["payload"]["i"] = 999  # rewrite history
        lines[1] = json.dumps(record)
        path.write_text("\n".join(lines) + "\n")

        reopened = JSONLLedger(path)
        v = reopened.verify_chain()
        assert not v.valid and v.first_invalid_seq == 2
        assert "modified" in v.reason
        reopened.close()

    def test_corrupt_line_raises_on_open(self, tmp_path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text('{"seq": 1, "broken json...\n')
        with pytest.raises(LedgerError):
            JSONLLedger(path)

    def test_ledger_failure_fails_engine_closed(self, tmp_path, monkeypatch) -> None:
        ledger = JSONLLedger(tmp_path / "a.jsonl")
        ledger.close()

        def explode(*a, **kw):
            raise LedgerError("disk full")

        monkeypatch.setattr(ledger, "append", explode)
        engine = CASEEngine(
            config=EngineConfig(ledger_backend="memory"),
            llm_client=MockLLMClient(), ledger=ledger,
        )
        with pytest.raises(LedgerError):
            engine.run("any query")


class TestSQLiteLedger:
    def test_append_and_verify(self, tmp_path) -> None:
        ledger = SQLiteLedger(tmp_path / "audit.sqlite3")
        _append_three(ledger)
        assert ledger.verify_chain().valid
        ledger.close()

    def test_sql_queryable(self, tmp_path) -> None:
        import sqlite3

        path = tmp_path / "audit.sqlite3"
        ledger = SQLiteLedger(path)
        _append_three(ledger)
        ledger.close()
        con = sqlite3.connect(path)
        rows = con.execute(
            "SELECT event_type, COUNT(*) FROM audit_events GROUP BY event_type"
        ).fetchall()
        con.close()
        assert rows == [("SESSION_START", 3)]

    def test_tampering_detected(self, tmp_path) -> None:
        import sqlite3

        path = tmp_path / "audit.sqlite3"
        ledger = SQLiteLedger(path)
        _append_three(ledger)
        ledger.close()
        con = sqlite3.connect(path)
        con.execute("UPDATE audit_events SET payload = '{\"i\": 42}' WHERE seq = 2")
        con.commit()
        con.close()
        reopened = SQLiteLedger(path)
        v = reopened.verify_chain()
        assert not v.valid and v.first_invalid_seq == 2
        reopened.close()


class TestFactory:
    def test_unknown_backend(self) -> None:
        with pytest.raises(ConfigurationError):
            create_ledger("blockchain")

    def test_memory_backend(self) -> None:
        assert isinstance(create_ledger("memory"), InMemoryLedger)

    def test_default_paths(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        ledger = create_ledger("jsonl")
        assert str(ledger._path) == "case_audit.jsonl"
        ledger.close()
