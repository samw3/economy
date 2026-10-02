"""Smoke tests for every CLI subcommand."""

import json

from econ.cli import main

from .conftest import EXAMPLES

SV = str(EXAMPLES / "starter_valley.yaml")
BROKEN = str(EXAMPLES / "broken_economy.yaml")


def test_lint_exit_codes(capsys):
    assert main(["lint", SV, "--no-lean", "--no-sim"]) == 0
    assert main(["lint", BROKEN, "--no-lean", "--no-sim"]) == 1
    out = capsys.readouterr().out
    assert "money-pump" in out and "fix:" in out


def test_lint_json(capsys):
    main(["lint", BROKEN, "--no-lean", "--no-sim", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["errors"] > 0
    assert all({"code", "severity", "message", "path"} <= f.keys() for f in data["findings"])


def test_sim_variants(capsys, tmp_path):
    assert main(["sim", SV, "-n", "120", "--runs", "3"]) == 0
    assert "Player evolution" in capsys.readouterr().out
    trace = tmp_path / "t.jsonl"
    assert main(["sim", SV, "-n", "50", "--runs", "2", "--level", "6", "--location", "crypt",
                 "--money", "{coin: 2s}", "--items", "iron_sword=1", "--policy", "craft=none",
                 "--trace", str(trace), "--format", "json", "--log", "5"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["encounters"] == 50 and len(data["log"]) <= 5
    rows = [json.loads(l) for l in trace.read_text().splitlines()]
    assert len(rows) == 100 and rows[0]["location"] == "crypt"


def test_tables_explain_schema_new(capsys, tmp_path):
    assert main(["tables", SV]) == 0
    out = capsys.readouterr().out
    assert "Battle odds" in out and "Shadow prices" in out
    assert main(["explain", "death-farming"]) == 0
    assert "respawn" in capsys.readouterr().out.lower()
    assert main(["explain", "formulas"]) == 0
    assert "playerLevel" in capsys.readouterr().out
    assert main(["explain", "nope-rule"]) == 1
    capsys.readouterr()
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "items" in schema["properties"]
    new = tmp_path / "new.yaml"
    assert main(["new", str(new)]) == 0
    capsys.readouterr()
    assert main(["lint", str(new), "--no-lean", "--no-sim"]) == 0


def test_diff(capsys, tmp_path):
    b = tmp_path / "b.yaml"
    b.write_text((EXAMPLES / "starter_valley.yaml").read_text().replace("wallet_cap: 20g", "wallet_cap: 2g"))
    assert main(["diff", SV, str(b), "--no-sim"]) == 0
    assert "wallet" in capsys.readouterr().out
