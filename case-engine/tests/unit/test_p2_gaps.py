"""Tests for the P2 gap closures (G14, G15, G16, G17, G19; v2.3.0).

Every test is hermetic: tmp-path files, in-memory backends, the offline
mock provider, no network.  These tests are the regression contract for
the v2.3.0 P2 closure round documented in ``docs/GAP_ANALYSIS.md``.

Not directly tested here (and why):
* **G18** (CONTRIBUTING/SECURITY/LICENSE) — documentation artifacts;
  ``test_g14_ci_and_tooling`` asserts their presence instead.
* **G20** (Python floor decision) — a documented decision, verified by
  ``requires-python`` staying ``>=3.11`` in the packaging test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from case_engine import CASEEngine, EngineConfig, GlobalGuardrails
from case_engine.exceptions import RegistryError, RegistryPackError
from case_engine.logging_setup import configure_logging
from case_engine.messages import (
    DictMessageCatalog,
    get_catalog,
    msg,
    reset_catalog,
    set_catalog,
)
from case_engine.registry import CognitiveRegistry, load_registry_pack
from case_engine.schemas.pipeline import CASEResult
from case_engine.storage import SQLiteResultStore
from case_engine.version import SCHEMA_VERSION, __version__

# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _engine(tmp_path: Path, **config_kw) -> CASEEngine:
    """Build an offline engine with a temp JSONL ledger."""
    return CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=2),
            ledger_backend="jsonl",
            ledger_path=str(tmp_path / "audit.jsonl"),
            **config_kw,
        ),
    )


@pytest.fixture(autouse=True)
def _clean_catalog():
    """Guarantee the message seam is inert for every test (G19)."""
    reset_catalog()
    yield
    reset_catalog()


@pytest.fixture()
def quiet():
    """Silence engine INFO logs for readable test output."""
    configure_logging(level="CRITICAL")
    yield
    configure_logging(level="CRITICAL")


# --------------------------------------------------------------------- #
# G14 — CI pipeline + lint/type gates
# --------------------------------------------------------------------- #


class TestG14CITooling:
    """The CI gate and its configuration exist and are coherent."""

    def test_ci_workflow_declares_all_gates(self):
        workflow = Path(".github/workflows/ci.yml")
        assert workflow.exists(), "CI workflow missing"
        text = workflow.read_text(encoding="utf-8")
        for gate in ("ruff check", "mypy case_engine/", "pytest tests/",
                     "--doctest-modules", "compileall"):
            assert gate in text, f"CI workflow missing gate: {gate}"
        for version in ("3.11", "3.12", "3.13"):
            assert version in text, f"CI matrix missing Python {version}"

    def test_tool_config_present_in_pyproject(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.ruff" in pyproject
        assert "[tool.mypy]" in pyproject

    def test_doc_hygiene_files_shipped(self):
        # G18 evidence, asserted where CI can see it.
        for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "LICENSE"):
            assert Path(name).exists(), f"{name} missing"


# --------------------------------------------------------------------- #
# G15 — external JSON registry packs
# --------------------------------------------------------------------- #


class TestG15RegistryPacks:
    """The JSON pack contract: round trip, overlay, fail-closed loads."""

    def test_dump_load_round_trip(self, tmp_path):
        default = CognitiveRegistry.load_default()
        path = tmp_path / "pack.json"
        default.dump_pack(path)
        reloaded = CognitiveRegistry.load_pack(path)
        assert len(reloaded) == 15
        assert [m.algorithm_id for m in reloaded.all_models()] == [
            m.algorithm_id for m in default.all_models()
        ]
        assert reloaded.get_by_tool_name(
            default.get("MM005").tool_name).algorithm_id == "MM005"

    def test_dumped_document_shape(self, tmp_path):
        doc = CognitiveRegistry.load_default().dump_pack(tmp_path / "p.json")
        assert set(doc) == {"schema_version", "generated_by",
                            "models", "rules", "routing_matrix"}
        assert doc["schema_version"] == SCHEMA_VERSION
        assert len(doc["models"]) == 15 and len(doc["rules"]) == 16
        assert (tmp_path / "p.json").exists()

    def test_overlay_tunes_one_model(self, tmp_path):
        doc = CognitiveRegistry.load_default().dump_pack()
        mm005 = next(m for m in doc["models"] if m["algorithm_id"] == "MM005")
        mm005["confidence_threshold"] = 0.99
        path = tmp_path / "overlay.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        tuned = CognitiveRegistry.load_pack(path, overlay=True)
        assert tuned.get("MM005").confidence_threshold == 0.99
        assert len(tuned) == 15  # everything else unchanged

    def test_load_registry_pack_function_twin(self, tmp_path):
        path = tmp_path / "pack.json"
        CognitiveRegistry.load_default().dump_pack(path)
        models, rules, matrix = load_registry_pack(path)
        assert len(models) == 15 and len(rules) == 16 and matrix

    @pytest.mark.parametrize("mutate,fragment", [
        (lambda d: d.update(schema_version="0.0.1"), "schema_version"),
        (lambda d: d.update(unknown_key=1), "unknown top-level keys"),
        (lambda d: d.pop("rules"), "missing the 'rules' section"),
        (lambda d: d.update(routing_matrix=[]), "routing_matrix must be an object"),
        (lambda d: d["models"][0].update(algorithm_id="MM9999"), "failed validation"),
    ])
    def test_tampered_packs_fail_closed(self, tmp_path, mutate, fragment):
        doc = CognitiveRegistry.load_default().dump_pack()
        mutate(doc)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(RegistryPackError, match=fragment) as excinfo:
            CognitiveRegistry.load_pack(path)
        assert excinfo.value.context["pack_path"] == str(path)

    def test_invalid_json_and_missing_file(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(RegistryPackError, match="not valid JSON"):
            CognitiveRegistry.load_pack(bad)
        with pytest.raises(RegistryPackError, match="Cannot read"):
            CognitiveRegistry.load_pack(tmp_path / "missing.json")

    def test_pack_error_is_registry_error(self):
        # Callers catching RegistryError keep working (hierarchy contract).
        assert issubclass(RegistryPackError, RegistryError)

    def test_inconsistent_pack_fails_consistency_check(self, tmp_path):
        doc = CognitiveRegistry.load_default().dump_pack()
        doc["models"][0]["validation_rules"] = ["VR999"]  # dangling rule ref
        path = tmp_path / "inconsistent.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(RegistryError, match="unknown validation rule"):
            CognitiveRegistry.load_pack(path)


# --------------------------------------------------------------------- #
# G16 — optional FastAPI serving layer
# --------------------------------------------------------------------- #

fastapi = pytest.importorskip("fastapi", reason="server extra not installed")
from fastapi.testclient import TestClient

from case_engine.server import RunRequest, create_app


class TestG16Server:
    """HTTP surface: run, fetch, verify, and typed rejections."""

    @pytest.fixture()
    def client(self, tmp_path, quiet):
        engine = _engine(tmp_path)
        with TestClient(create_app(engine)) as c:
            yield c

    def test_health_reports_engine_identity(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["engine_version"] == __version__
        assert body["models"] == 15

    def test_run_and_fetch_round_trip(self, client):
        created = client.post("/sessions", json={"query": "Dashboard shows stale data"})
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        fetched = client.get(f"/sessions/{session_id}")
        assert fetched.status_code == 200
        assert fetched.json()["session_id"] == session_id

    def test_unknown_session_is_404(self, client):
        assert client.get("/sessions/does-not-exist").status_code == 404

    def test_ledger_verify_endpoint(self, client):
        assert client.get("/ledger/verify").json()["valid"] is True

    def test_rejects_unknown_fields_and_empty_query(self, client):
        assert client.post(
            "/sessions", json={"query": "x", "bogus": 1}).status_code == 422
        assert client.post("/sessions", json={"query": ""}).status_code == 422

    def test_run_request_model_is_strict(self):
        with pytest.raises(Exception):
            RunRequest.model_validate({"query": "x", "unexpected": True})

    def test_mirror_is_bounded(self):
        from case_engine.server import _SessionMirror
        mirror = _SessionMirror(capacity=2)
        for i in range(5):
            mirror.put(CASEResult(session_id=f"s{i}", user_query="q"))
        assert mirror.get("s4") is not None
        assert mirror.get("s0") is None  # evicted FIFO

    def test_create_app_missing_fastapi_message(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("fastapi"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match=r"case-engine\[server\]"):
            create_app()


# --------------------------------------------------------------------- #
# G17 — CLI doctor / init / export
# --------------------------------------------------------------------- #


class TestG17CliVerbs:
    """The three new operator verbs behave and exit-code correctly."""

    def _main(self, *argv):
        from case_engine.cli import main
        return main(list(argv))

    def test_doctor_healthy_default(self, capsys, quiet):
        code = self._main("doctor", "--json",
                          "--ledger-path", "missing-fresh-install.jsonl")
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["healthy"] is True
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        assert statuses["registry"] == "ok"
        assert statuses["ledger"] == "warn"  # fresh install
        assert statuses["provider"] == "ok"

    def test_doctor_detects_tampered_ledger(self, tmp_path, capsys, quiet):
        path = tmp_path / "audit.jsonl"
        _engine(tmp_path).run("Dashboard shows stale data")
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["payload"]["tampered"] = True
        lines[0] = json.dumps(event, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        code = self._main("doctor", "--ledger-path", str(path))
        assert code == 1
        assert "TAMPERED" in capsys.readouterr().out

    def test_init_writes_and_validates_template(self, tmp_path, capsys, quiet):
        target = tmp_path / "config.json"
        assert self._main("init", "--path", str(target)) == 0
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["provider"] == "mock"
        # The template must round-trip through the strict loader.
        from case_engine.config import EngineConfig
        EngineConfig.from_file(target)  # must not raise

    def test_init_refuses_overwrite_without_force(self, tmp_path, capsys):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        assert self._main("init", "--path", str(target)) == 1
        assert self._main("init", "--path", str(target), "--force") == 0

    def _seed_store(self, path: Path) -> None:
        store = SQLiteResultStore(path)
        store.save(CASEResult(session_id="s1", user_query="q1",
                              domain_classification="Complex",
                              selected_model_ids=["MM001"]))
        store.save(CASEResult(session_id="s2", user_query="q2", escalated=True))
        store.close()

    def test_export_json_and_csv(self, tmp_path, capsys, quiet):
        store_path = tmp_path / "results.sqlite3"
        self._seed_store(store_path)
        out_path = tmp_path / "out"
        assert self._main("export", "--store", str(store_path),
                          "--format", "json", "--out", str(out_path / "r.json")) == 0
        docs = json.loads((out_path / "r.json").read_text(encoding="utf-8"))
        assert {d["session_id"] for d in docs} == {"s1", "s2"}
        assert self._main("export", "--store", str(store_path),
                          "--format", "csv", "--out", str(out_path / "r.csv")) == 0
        csv_text = (out_path / "r.csv").read_text(encoding="utf-8")
        assert csv_text.splitlines()[0].startswith("session_id,")
        assert "s2" in csv_text

    def test_export_empty_store_fails_cleanly(self, tmp_path, capsys):
        code = self._main("export", "--store", str(tmp_path / "empty.sqlite3"))
        assert code == 1


# --------------------------------------------------------------------- #
# G19 — message-catalog seam
# --------------------------------------------------------------------- #


class TestG19Messages:
    """The i18n seam is inert by default and safe under every failure."""

    def test_inert_by_default(self):
        assert get_catalog() is None
        assert msg("vr.VR001.fail", default="English") == "English"

    def test_catalog_overrides_and_params(self):
        set_catalog(DictMessageCatalog({
            "greeting": "Hi {name}",
            "plain": "flat",
        }))
        assert msg("greeting", default="English", name="Ada") == "Hi Ada"
        assert msg("plain", default="English") == "flat"

    def test_missing_key_falls_back(self):
        set_catalog(DictMessageCatalog({"other": "x"}))
        assert msg("vr.VR001.fail", default="English") == "English"

    def test_format_failure_returns_default(self):
        set_catalog(DictMessageCatalog({"k": "Hi {name}"}))
        assert msg("k", default="English") == "English"  # no params -> raw? no:
        # A template with placeholders and NO params is returned unformatted
        # only when params are absent; with params missing it falls back.
        assert msg("k", default="English", unrelated=1) == "English"

    def test_throwing_catalog_never_breaks_engine(self):
        class Exploding:
            def get(self, key: str) -> str | None:
                raise RuntimeError("catalog storage down")

        set_catalog(Exploding())
        assert msg("vr.VR001.fail", default="English") == "English"

    def test_reset_restores_english(self):
        set_catalog(DictMessageCatalog({"k": "translated"}))
        reset_catalog()
        assert msg("k", default="English") == "English"

    def test_validation_report_uses_catalog(self, tmp_path, quiet):
        from case_engine.validation.rules import run_rule

        engine = _engine(tmp_path)
        set_catalog(DictMessageCatalog({
            "vr.VR001.fail": "VR001-ERROR: no observation recorded",
            "vr.VR001.pass": "VR001-OK",
        }))
        model_def = engine.registry.get("MM001")
        guardrails = engine.config.guardrails

        class Trace:
            orient_context = "The weather is lovely"
            observe_raw_data = ["Sensor A 95% moisture"]

        failed = run_rule("VR001", Trace(), model_def, guardrails)
        assert not failed.passed
        assert failed.message == "VR001-ERROR: no observation recorded"

        class GoodTrace:
            orient_context = "Sensor logs show 95% moisture"
            observe_raw_data = ["Sensor A 95% moisture"]

        passed = run_rule("VR001", GoodTrace(), model_def, guardrails)
        assert passed.passed
        assert passed.message == "VR001-OK"

    def test_escalation_message_uses_catalog(self, tmp_path, quiet):
        engine = _engine(tmp_path)
        set_catalog(DictMessageCatalog({
            "escalation.infra_failure": "escalation error_type={error_type} session={session_id}",
        }))
        result = engine._escalation_result(
            {"user_query": "q", "retry_count": 0, "started_at": "t"},
            "sess-123", RuntimeError("boom"), 0,
        )
        assert result.escalated
        assert result.final_response == (
            "escalation error_type=RuntimeError session=sess-123")

    def test_escalation_message_default_unchanged(self, tmp_path, quiet):
        engine = _engine(tmp_path)
        result = engine._escalation_result(
            {"user_query": "q", "retry_count": 0, "started_at": "t"},
            "sess-123", RuntimeError("boom"), 0,
        )
        assert "hit an internal failure" in result.final_response
        assert "sess-123" in result.final_response


# --------------------------------------------------------------------- #
# G20 — documented compatibility decision
# --------------------------------------------------------------------- #


class TestG20CompatDecision:
    """The floor decision is enforced (3.11+) and documented."""

    def test_python_floor_unchanged(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        assert 'requires-python = ">=3.11"' in pyproject
