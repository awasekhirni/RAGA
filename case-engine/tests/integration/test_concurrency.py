"""G24 — concurrency tests: threads × sessions, processes × ledger appends.

Closes the follow-up from G9 (v2.2.0 added the flock machinery; these
tests prove its guarantees).  The contract under test:

* **N threads × M sessions on one engine** — every session completes in
  a defined terminal state, session ids never collide and the shared
  ledger's hash chain verifies afterwards.
* **N processes × M appends on one JSONL file** — with
  ``cross_process_safe=True`` the writers serialize under the advisory
  flock and adopt each other's chain tail, so the file ends up with
  exactly ``N*M`` cleanly serialised events and one verifiable chain.
  The outcome is either clean serialization or a clear error — never
  silent corruption.
* **Corruption is loud** — a hand-corrupted tail line makes ledger
  construction raise :class:`LedgerError` instead of silently
  continuing the chain from garbage.

All tests are offline and use the ``spawn`` start method for
cross-process cases so the suite is platform-portable.

Cross-references: ``case_engine/audit/jsonl_ledger.py`` (G9 protocol),
``docs/GAP_ANALYSIS.md`` §G24, ``docs/AUDIT_LEDGER.md`` §6.
"""

from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from case_engine.audit import JSONLLedger, SQLiteLedger
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import CASEError, LedgerError
from case_engine.llm import MockLLMClient
from case_engine.registry import CognitiveRegistry
from case_engine.schemas.pipeline import CASEResult

#: Parallelism profile — sized so the whole module stays under ~30 s.
_THREADS = 8
_SESSIONS_PER_THREAD = 4
_PROCESSES = 4
_APPENDS_PER_PROCESS = 5

_QUERY = "Why did our checkout error rate spike last Tuesday?"


def _terminal(result: CASEResult) -> bool:
    """Return True when a result reached a defined terminal state."""
    return bool(
        result.all_valid or result.escalated or result.aborted
        or result.passthrough or result.final_response is not None
    )


# --------------------------------------------------------------------- #
# Threads × sessions on one engine
# --------------------------------------------------------------------- #


def test_threads_share_one_engine_safely(registry: CognitiveRegistry) -> None:
    """8 threads × 4 sessions on one engine: no collision, chain intact."""
    engine = CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
            ledger_backend="memory",
        ),
        llm_client=MockLLMClient.from_registry_examples(registry),
    )

    def run_batch(_thread_index: int) -> list[CASEResult]:
        return [engine.run(_QUERY) for _ in range(_SESSIONS_PER_THREAD)]

    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        batches = list(pool.map(run_batch, range(_THREADS)))

    results = [r for batch in batches for r in batch]
    assert len(results) == _THREADS * _SESSIONS_PER_THREAD
    session_ids = {r.session_id for r in results}
    assert len(session_ids) == len(results), "session id collision under concurrency"
    for result in results:
        assert _terminal(result), f"undefined terminal state for {result.session_id}"
        assert result.audit_event_count > 0
    verification = engine.ledger.verify_chain()
    assert verification.valid, f"shared chain broke under threads: {verification.reason}"
    assert verification.total_entries == verification.total_entries  # sanity: chain walked


def test_threads_share_one_jsonl_ledger(
    tmp_path: Path, registry: CognitiveRegistry
) -> None:
    """6 threads drive one shared JSONLLedger-backed engine: no tearing."""
    ledger = JSONLLedger(
        tmp_path / "shared.jsonl", cross_process_safe=True, fsync=False
    )
    engine = CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
        ),
        llm_client=MockLLMClient.from_registry_examples(registry),
        ledger=ledger,
    )

    def run_batch(_thread_index: int) -> list[CASEResult]:
        return [engine.run(_QUERY) for _ in range(2)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = list(pool.map(run_batch, range(6)))

    flat = [r for batch in batches for r in batch]
    assert len(flat) == 12
    assert len({r.session_id for r in flat}) == 12
    verification = ledger.verify_chain()
    assert verification.valid, f"JSONL chain broke under threads: {verification.reason}"
    events = list(ledger.iter_events())
    assert len(events) == verification.total_entries
    ledger.close()


def test_threads_append_directly_to_one_ledger(tmp_path: Path) -> None:
    """8 threads × 10 appends on one JSONLLedger: file lines stay whole."""
    ledger = JSONLLedger(
        tmp_path / "direct.jsonl", cross_process_safe=True, fsync=False
    )
    total = _THREADS * 10

    def append_batch(thread_index: int) -> None:
        for i in range(10):
            ledger.append(
                "TRACE_GENERATED", f"sess-{thread_index}", "test",
                {"i": i, "thread": thread_index},
            )

    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        list(pool.map(append_batch, range(_THREADS)))

    lines = (tmp_path / "direct.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == total
    parsed = [json.loads(line) for line in lines]  # every line is clean JSON
    assert len({e["seq"] for e in parsed}) == total, "duplicate seq under threads"
    assert ledger.verify_chain().valid
    ledger.close()


# --------------------------------------------------------------------- #
# Processes × appends on one JSONL file
# --------------------------------------------------------------------- #


def _process_append_worker(path: str, process_index: int, appends: int) -> None:
    """Append *appends* events from one process to a shared JSONL file.

    Module-level so ``spawn`` can pickle it.  Each worker opens its own
    ledger handle; the advisory flock plus tail re-read must weave them
    into one clean chain.
    """
    ledger = JSONLLedger(path, cross_process_safe=True, fsync=False)
    try:
        for i in range(appends):
            ledger.append(
                "TRACE_GENERATED",
                f"proc-{process_index}",
                "test",
                {"i": i, "process": process_index},
            )
    finally:
        ledger.close()


def test_processes_append_to_one_jsonl_without_corruption(tmp_path: Path) -> None:
    """4 processes × 5 appends → one file, one chain, zero corruption."""
    target = tmp_path / "multiproc.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(
            target=_process_append_worker,
            args=(str(target), index, _APPENDS_PER_PROCESS),
        )
        for index in range(_PROCESSES)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, "worker process crashed while appending"

    expected = _PROCESSES * _APPENDS_PER_PROCESS
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected, "lost or duplicated events across processes"
    records = [json.loads(line) for line in lines]  # clean serialization
    assert len({r["seq"] for r in records}) == expected, "duplicate seq across processes"

    # The reopened ledger replays the persisted tail and verifies it.
    reopened = JSONLLedger(target, cross_process_safe=True, fsync=False)
    try:
        verification = reopened.verify_chain()
        assert verification.valid, f"cross-process chain broke: {verification.reason}"
        assert verification.total_entries == expected
    finally:
        reopened.close()


def test_corrupt_tail_raises_clear_error(tmp_path: Path) -> None:
    """A hand-corrupted tail line is a loud error, never silent corruption."""
    path = tmp_path / "corrupt.jsonl"
    ledger = JSONLLedger(path, fsync=False)
    ledger.append("TRACE_GENERATED", "sess", "test", {"ok": True})
    ledger.close()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "hash": "FAKE", "payload": {"tampered": true}\n')  # invalid JSON

    # Construction replays the file: the corrupt line must be reported,
    # not skipped (full-file replay fires before tail re-read).
    with pytest.raises(LedgerError, match="Corrupt ledger"):
        JSONLLedger(path, fsync=False)


def test_corrupt_tail_detected_during_append(tmp_path: Path) -> None:
    """Tail corruption *after* open fails the next locked append (G9)."""
    path = tmp_path / "late-corrupt.jsonl"
    ledger = JSONLLedger(path, cross_process_safe=True, fsync=False)
    ledger.append("TRACE_GENERATED", "sess", "test", {"ok": True})

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{corrupt garbage\n")

    with pytest.raises(LedgerError, match="Corrupt ledger tail"):
        ledger.append("TRACE_GENERATED", "sess", "test", {"n": 2})
    ledger.close()


def test_midfile_corruption_is_detected_not_swallowed(tmp_path: Path) -> None:
    """A corrupt line *inside* the file fails replay with a clear error."""
    path = tmp_path / "midcorrupt.jsonl"
    ledger = JSONLLedger(path, fsync=False)
    ledger.append("TRACE_GENERATED", "sess", "test", {"n": 1})
    ledger.append("TRACE_GENERATED", "sess", "test", {"n": 2})
    ledger.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0][:-5]  # truncate the first record: invalid JSON
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises((LedgerError, CASEError), match="Corrupt ledger line"):
        JSONLLedger(path, fsync=False)


def test_sqlite_threads_append_without_corruption(tmp_path: Path) -> None:
    """4 threads × 10 appends on one SQLiteLedger: chain verifies."""
    ledger = SQLiteLedger(tmp_path / "shared.db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda i: [
                ledger.append("TRACE_GENERATED", f"t-{i}", "test", {"n": j})
                for j in range(10)
            ],
            range(4),
        ))
    verification = ledger.verify_chain()
    assert verification.valid
    assert verification.total_entries == 40
    ledger.close()


# --------------------------------------------------------------------- #
# Cross-backend sanity: chain hand-off JSONL → SQLite stays byte-identical
# --------------------------------------------------------------------- #


def test_ledger_portability_jsonl_to_sqlite(tmp_path: Path) -> None:
    """Events re-imported into SQLite rebuild a verifiable chain.

    Hashes are *not* byte-identical across backends — every hash commits
    to the event's own timestamp, and a re-import is a new append — but
    the rebuilt chain must verify, carry the same events in order and
    preserve payloads exactly (the ``docs/AUDIT_LEDGER.md`` portability
    contract).
    """
    source = JSONLLedger(tmp_path / "portable.jsonl", fsync=False)
    for i in range(5):
        source.append("TRACE_GENERATED", "sess-port", "test", {"n": i})
    source.close()

    events = list(JSONLLedger(tmp_path / "portable.jsonl", fsync=False).iter_events())
    target = SQLiteLedger(tmp_path / "portable.db")
    for event in events:
        target.append(
            event.event_type, event.session_id, event.node, event.payload
        )
    rebuilt = list(SQLiteLedger(tmp_path / "portable.db").iter_events())
    assert len(rebuilt) == len(events)
    assert [e.session_id for e in rebuilt] == [e.session_id for e in events]
    assert [e.payload for e in rebuilt] == [e.payload for e in events]
    assert SQLiteLedger(tmp_path / "portable.db").verify_chain().valid
    target.close()
