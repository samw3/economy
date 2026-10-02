"""Regression tests for issues found in code review."""

from fractions import Fraction

from econ.analysis.market import _solve_exact, grind_bounds, income_ceiling, loot_caps
from econ.economy import load_economy
from econ.lint.runner import lint
from econ.sim.engine import Simulator
from econ.sim.report import simulate


def _eco(write_yaml, text):
    eco, findings, _ = load_economy(write_yaml(text))
    assert eco is not None, [f.message for f in findings]
    return eco


LUCKY = """
currencies: {coin: {}}
items: {junk: {sell: 1}, prize: {sell: 100}}
recipes: {roll: {inputs: {junk: 1}, outputs: {prize: 1}, success_chance: 0.1}}
enemies: {rat: {level: 1, xp: 1, drops: [{item: junk}]}}
locations: {town: {safe: true}, field: {enemies: [{enemy: rat}]}}
battle: {formula: 0.5}
progression: {max_level: 2, xp_to_next: 10}
"""


def test_any_luck_bounds_use_lucky_crafts(write_yaml):
    eco = _eco(write_yaml, LUCKY)
    # one kill + one lucky roll yields a prize: no bound above 1 may be claimed
    b = grind_bounds(eco).get("prize")
    assert b is None or b.events <= 1
    # one kill can be worth a whole prize (100) to a lucky player
    ic = income_ceiling(eco)
    assert ic is not None and ic.D >= 100


def test_solve_exact_ignores_zero_coefficients():
    x = _solve_exact([({0: Fraction(0), 1: Fraction(-1)}, Fraction(0))], 2, [Fraction(0)] * 2)
    assert x is not None and x[1] == 0


def test_overflow_is_not_income(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  vat: {sell: 40c}") + """
recipes:
  replicate: {inputs: {vat: 1}, outputs: {vat: 2}, cost: 1c}
player:
  start: {items: {vat: 1}, currencies: {coin: 10c}}
"""
    eco = _eco(write_yaml, text)
    res = simulate(eco, 200, runs=2, seed=1)
    text_out = res.text()
    plus = next(l for l in text_out.splitlines() if l.strip().startswith("coin: +"))
    assert "overflow" not in plus
    assert "wasted by wallet cap" in text_out


def test_hours_target_without_level_ranges(write_yaml, base_yaml):
    text = base_yaml.replace("    level_range: [1, 3]\n", "") + "targets: {hours_to_max_level: [1, 10]}\n"
    rep = lint(write_yaml(text), lean=False, sim=False)
    assert not any(f.code == "internal-error" for f in rep.findings), [f.message for f in rep.findings]
    assert rep.metrics["leveling"]["hours_to_max"] is None


def test_hours_use_location_rate(write_yaml, base_yaml):
    text = base_yaml.replace("    level_range: [1, 3]\n", "    level_range: [1, 5]\n    encounter_rate: 4\n")
    rep = lint(write_yaml(text), lean=False, sim=False)
    lv = rep.metrics["leveling"]
    assert lv["hours_to_max"] is not None
    assert abs(lv["hours_to_max"] - lv["encounters_to_max"] / 4 / 60) < 1e-9


def test_death_farming_from_currency_formula(write_yaml, base_yaml):
    text = base_yaml + "respawn:\n  currencies: {coin: \"max(amount, 50)\"}\n"
    eco = _eco(write_yaml, text)
    caps = dict(loot_caps(eco))
    assert caps["respawn (grants and currency formulas)"]["coin"] == 50
    rep = lint(write_yaml(text), lean=False, sim=False)
    df = [f for f in rep.findings if f.code == "death-farming"]
    assert df and df[0].path == "respawn.currencies.coin"


def test_level_loss_unequips_unusable_gear(write_yaml, base_yaml):
    text = base_yaml.replace("power: 1, half_life: 100}", "power: 1, half_life: 100, min_level: 3}") + \
        "respawn:\n  level: max(1, level - 3)\n"
    eco = _eco(write_yaml, text)
    sim = Simulator(eco, seed=1)
    p = sim.new_player(level=4, items={"sword": 1})
    assert p.equipped.get("weapon") == "sword"
    from econ.sim.engine import RunStats
    sim._die(p, RunStats(), 1, "meadow", "slime", 1)
    assert p.level == 1 and "weapon" not in p.equipped and p.inv["sword"] == 1
    assert sim.power(p) == 0


def test_spawn_study_unreachable_immortality(write_yaml, base_yaml):
    from econ.analysis.combat import spawn_study
    # enemies give no XP: the player is stuck at level 1 and is mortal there, even though
    # the formula would make max-level players immortal
    text = base_yaml.replace("xp: 5,", "xp: 0,").replace("xp: 10,", "xp: 0,").replace(
        "clamp(sigmoid((delta + 2) / 1.2), 0.01, 0.99)", "clamp(0.5 + 0.2 * delta, 0, 1)")
    eco = _eco(write_yaml, text)
    s = spawn_study(eco, eco.locations["meadow"], 1, "naked")
    assert s.lifespan < float("inf") and "progress stops" in s.note


def test_pump_capital_counts_inputs_of_duplication(write_yaml, base_yaml):
    from econ.analysis.market import analyze_market
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  seed: {slot: charm, power: 1}") + """
recipes:
  sprout: {inputs: {seed: 1}, outputs: {seed: 2}}
"""
    eco = _eco(write_yaml, text)
    p = next(p for p in analyze_market(eco).pumps if p.kind == "duplication")
    assert p.capital == {"seed": 1}


def test_flow_mapping_comma_hint(write_yaml, base_yaml):
    eco, findings, _ = load_economy(write_yaml(base_yaml + "respawn:\n  currencies: {coin: max(amount, 50)}\n"))
    assert eco is None
    assert any("quote it" in f.message for f in findings)
