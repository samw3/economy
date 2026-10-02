"""End-to-end: lint rules on the examples, the simulator, and Lean verification."""

import json
import shutil

import pytest

from econ.economy import load_economy
from econ.lint.report import to_json, to_text
from econ.lint.runner import lint
from econ.proofs.runner import find_lake, lean_dir
from econ.sim.report import simulate

from .conftest import EXAMPLES

HAS_LEAN = find_lake() is not None and (lean_dir() / "lakefile.toml").exists()


def codes(rep):
    return {f.code for f in rep.findings}


def test_starter_valley_is_clean():
    rep = lint(str(EXAMPLES / "starter_valley.yaml"), lean=False)
    bad = [f for f in rep.findings if f.severity in ("error", "warning")]
    assert not bad, to_text(rep)
    m = rep.metrics
    assert m["market"]["status"].startswith("no pump")
    assert m["quick_sim"]["final_level_p50"] >= 1


EXPECTED_BROKEN = {
    "buy-below-sell", "death-farming", "free-item", "money-pump", "odds-range", "unaffordable",
    "death-spiral", "immortal", "level-gap", "leveling-pace", "lifespan-short", "no-sink",
    "penalty-evasion", "risk-reward-inversion", "unobtainable-item", "upkeep-heavy", "wallet-overflow",
    "dead-item", "unused",
}


def test_broken_economy_findings():
    rep = lint(str(EXAMPLES / "broken_economy.yaml"), lean=False, sim=False)
    missing = EXPECTED_BROKEN - codes(rep)
    assert not missing, f"missing {missing}\n" + to_text(rep)
    pumps = [f for f in rep.findings if f.code == "money-pump"]
    assert {f.path for f in pumps} == {"recipes.smelt", "recipes.replicate"}
    for f in rep.findings:
        assert f.line is None or f.line > 0
    data = json.loads(to_json(rep))
    assert data["summary"]["errors"] == rep.count("error")


def test_ignore_rules(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  junk: {}") + """
lint:
  ignore:
    - unobtainable-item
    - {code: dead-item, path: items.junk}
"""
    rep = lint(write_yaml(text), lean=False, sim=False)
    assert "unobtainable-item" not in codes(rep)
    assert rep.ignored >= 1


def test_lifespan_long_and_even_fight(write_yaml, base_yaml):
    text = base_yaml.replace("clamp(sigmoid((delta + 2) / 1.2), 0.01, 0.99)", "clamp(0.995 + 0.001 * delta, 0, 0.9995)")
    rep = lint(write_yaml(text), lean=False, sim=False)
    assert {"lifespan-long", "even-fight-odds"} <= codes(rep), to_text(rep)


def test_odds_not_monotone(write_yaml, base_yaml):
    text = base_yaml.replace("clamp(sigmoid((delta + 2) / 1.2), 0.01, 0.99)", "clamp(0.5 + 0.3 * sin_free(delta), 0.01, 0.99)")
    text = text.replace("sin_free(delta)", "(1 if delta > 2 else -1 if delta > 0 else 1)")
    rep = lint(write_yaml(text), lean=False, sim=False)
    assert "odds-not-monotone" in codes(rep), to_text(rep)


def test_simulation_deterministic_and_capped():
    eco, _, _ = load_economy(str(EXAMPLES / "starter_valley.yaml"))
    a = simulate(eco, 400, runs=4, seed=3).summary()
    b = simulate(eco, 400, runs=4, seed=3).summary()
    assert a == b
    cap = eco.currencies[eco.primary].cap
    rows = []
    simulate(eco, 400, runs=2, seed=5, trace=rows.append)
    assert len(rows) == 800
    assert all(r["money"] <= cap for r in rows)
    deaths = [r for r in rows if r["outcome"] == "death"]
    assert all(r["level"] >= 1 for r in deaths)


def test_simulation_respawn_penalty():
    eco, _, _ = load_economy(str(EXAMPLES / "starter_valley.yaml"))
    # level-1 players stuck in the mountains die constantly; the penalty must drain money
    from econ.economy import Policy
    pol = Policy(location="mountains", buy_gear=False, craft="none")
    res = simulate(eco, 200, runs=3, seed=1, policy=pol, start={"level": 1, "money": {"coin": 10000}})
    s = res.summary()
    assert s["lives"]["deaths_total"] > 50
    assert s["flows_per_run"]["death penalty"]["coin"] < -5000
    assert s["items_per_run"]["granted"]["training_sword"] >= 1


def test_bot_exploits_money_pump(write_yaml, base_yaml):
    """The simulated player finds and uses a profitable recipe loop."""
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  vat: {sell: 40c}") + """
recipes:
  replicate: {inputs: {vat: 1}, outputs: {vat: 2}, cost: 1c}
player:
  start: {items: {vat: 1}, currencies: {coin: 10c}}
"""
    eco, findings, _ = load_economy(write_yaml(text))
    s = simulate(eco, 200, runs=2, seed=1).summary()
    assert s["final"]["money_p50"] == eco.currencies["coin"].cap  # pinned at the wallet cap


@pytest.mark.skipif(not HAS_LEAN, reason="Lean toolchain not installed")
def test_lean_verifies_examples():
    for name in ("starter_valley.yaml", "broken_economy.yaml"):
        rep = lint(str(EXAMPLES / name), lean=True, sim=False, use_cache=False)
        lean = rep.metrics["lean"]
        assert lean["ok"], lean
        assert lean["theorems"] and all(v == "proved" for v in lean["theorems"].values())
        assert set().union(*lean["axioms"].values()) <= {"propext", "Classical.choice", "Quot.sound"}
    rep = lint(str(EXAMPLES / "broken_economy.yaml"), lean=True, sim=False)
    pumps = [f for f in rep.findings if f.code in ("money-pump", "buy-below-sell", "free-item")]
    assert pumps and all(f.lean and f.lean.startswith("verified") for f in pumps)


@pytest.mark.skipif(not HAS_LEAN, reason="Lean toolchain not installed")
def test_lean_catches_a_false_claim(tmp_path):
    """Tamper with a generated claim: Lean must reject it (the pipeline is not a rubber stamp)."""
    from econ.lint.rules import Ctx, check_market
    from econ.proofs.generate import generate
    from econ.proofs.runner import check

    eco, _, _ = load_economy(str(EXAMPLES / "starter_valley.yaml"))
    ctx = Ctx(eco)
    check_market(ctx)
    mod = generate(eco, ctx, "TamperTest", source="tamper")
    victim = next(t for t in mod.theorems if t.kind == "lifespan")
    lines = mod.text.split("\n")
    line = lines[victim.start - 1]
    assert "(20 : Rat) ≤ lifespan" in line
    lines[victim.start - 1] = line.replace("(20 : Rat) ≤ lifespan", "(100000 : Rat) ≤ lifespan")
    # inflate a fastest-progression bound: N → N + 100 in both the statement and the check
    fast = next(t for t in mod.theorems if t.kind == "fastest")
    n = fast.meta["events"]
    for i in range(fast.start - 1, fast.end):
        lines[i] = lines[i].replace(f"→ {n} ≤ evs.countP", f"→ {n + 100} ≤ evs.countP").replace(
            f"startHoldings {n}", f"startHoldings {n + 100}")
    mod.text = "\n".join(lines)
    res = check(mod, use_cache=False)
    try:
        assert res.statuses[victim.name].startswith("failed")
        assert res.statuses[fast.name].startswith("failed")
        assert res.statuses["no_money_pump"] == "proved"
    finally:
        (lean_dir() / "Generated" / "TamperTest.lean").unlink(missing_ok=True)


def test_diff_reports_changes(tmp_path):
    from econ.lint.diff import diff_text
    src = (EXAMPLES / "starter_valley.yaml").read_text()
    b = tmp_path / "b.yaml"
    b.write_text(src.replace("  bias: 3.0 ", "  bias: 2.0 "))
    ra = lint(str(EXAMPLES / "starter_valley.yaml"), lean=False, sim=False)
    rb = lint(str(b), lean=False, sim=False)
    text = diff_text(ra, rb)
    assert "New (" in text and "lifespan-short" in text
    assert "meadow L1 lifespan geared" in text


def test_grind_bounds_are_sound():
    """A valuation's bound can never exceed what the simulator achieves (it is a lower bound)."""
    from econ.analysis.market import grind_bounds
    eco, _, _ = load_economy(str(EXAMPLES / "starter_valley.yaml"))
    gb = grind_bounds(eco)
    assert gb["steel_blade"].events >= 2
    res = simulate(eco, 1500, runs=10, seed=2).summary()
    for item, b in gb.items():
        acq = res["first_acquired"].get(item)
        if acq and acq["encounter_p50"] > 0:
            assert acq["encounter_p50"] >= b.events, (item, acq, b.events)
