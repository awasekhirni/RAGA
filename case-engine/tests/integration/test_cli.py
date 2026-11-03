"""Integration tests: the CLI surface (subprocess-free, in-process)."""

from __future__ import annotations

import json

from case_engine.cli import main


class TestModelsCommand:
    def test_lists_fifteen_models(self, capsys) -> None:
        assert main(["models"]) == 0
        out = capsys.readouterr().out
        assert "MM001" in out and "MM015" in out
        assert "OODA Loop" in out

    def test_domain_filter(self, capsys) -> None:
        assert main(["models", "--domain", "Chaotic"]) == 0
        out = capsys.readouterr().out
        assert "MM001" in out and "MM008" not in out


class TestShowCommand:
    def test_shows_full_entry(self, capsys) -> None:
        assert main(["show", "MM005"]) == 0
        entry = json.loads(capsys.readouterr().out)
        assert entry["algorithm_name"] == "Root Cause Analysis (5 Whys)"
        assert entry["validation_rules"] == ["VR005", "VR016"]


class TestRunCommand:
    def test_mock_run_offline(self, capsys, tmp_path) -> None:
        code = main([
            "run", "Dashboard shows stale data",
            "--provider", "mock", "--ledger", "jsonl",
            "--ledger-path", str(tmp_path / "cli.jsonl"), "--json",
        ])
        assert code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["all_valid"] is True

    def test_forced_models_flag(self, capsys, tmp_path) -> None:
        code = main([
            "run", "q", "--provider", "mock", "--ledger", "memory",
            "--models", "MM005", "--json", "--quiet",
        ])
        assert code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["selected_model_ids"] == ["MM005"]


class TestDemoCommand:
    def test_demo_end_to_end(self, capsys, tmp_path) -> None:
        ledger_path = tmp_path / "demo.jsonl"
        code = main(["demo", "--ledger-path", str(ledger_path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "C.A.S.E. analysis complete" in out
        assert "chain valid: True" in out

    def test_demo_with_reflexion(self, capsys, tmp_path) -> None:
        code = main(["demo", "--ledger-path", str(tmp_path / "d2.jsonl"), "--reflexion"])
        assert code == 0
        out = capsys.readouterr().out
        assert "retries=1" in out


class TestVerifyCommand:
    def test_verify_generated_ledger(self, capsys, tmp_path) -> None:
        path = tmp_path / "v.jsonl"
        main(["demo", "--ledger-path", str(path), "--quiet"])
        code = main(["verify", "--ledger-path", str(path)])
        assert code == 0
        assert "OK" in capsys.readouterr().out

    def test_verify_detects_tampering(self, capsys, tmp_path) -> None:
        path = tmp_path / "t.jsonl"
        main(["demo", "--ledger-path", str(path), "--quiet"])
        lines = path.read_text().strip().splitlines()
        record = json.loads(lines[0])
        record["payload"]["engine_version"] = "9.9.9"
        lines[0] = json.dumps(record)
        path.write_text("\n".join(lines) + "\n")
        code = main(["verify", "--ledger-path", str(path)])
        assert code == 1
        assert "TAMPERED" in capsys.readouterr().out


class TestConfigCommand:
    def test_prints_resolved_config(self, capsys) -> None:
        assert main(["config"]) == 0
        cfg = json.loads(capsys.readouterr().out)
        assert cfg["guardrails"]["max_reflexion_retries"] == 3
        assert cfg["guardrails"]["allow_unvalidated_passthrough"] is False
