"""Market (pumps/certificates), combat (lifespans) and respawn analyses."""

import random
from fractions import Fraction

import pytest

from econ.analysis.combat import encounter_stats, loadout, spawn_study
from econ.analysis.market import analyze_market, build_market
from econ.economy import load_economy


def _eco(write_yaml, text):
    eco, findings, _ = load_economy(write_yaml(text))
    assert eco is not None, [f.message for f in findings]
    return eco


def _verify_certificate(res):
    mk, p = res.market, res.prices
    for g in mk.goods:
        assert p[mk.index[g]] >= 0
        if g in mk.protected:
            assert p[mk.index[g]] > 0
    for a in mk.actions:
        assert sum(v * p[mk.index[g]] for g, v in a.delta.items()) <= 0


def test_certificate_is_exact(write_yaml, base_yaml):
    eco = _eco(write_yaml, base_yaml)
    res = analyze_market(eco)
    assert res.prices is not None and not res.pumps
    _verify_certificate(res)
    assert all(isinstance(x, Fraction) for x in res.prices)


def test_tight_exchange_cycle_certified(write_yaml, base_yaml):
    # buy/sell at the same price and a reversible exchange: everything is tight
    text = base_yaml + """
recipes:
  to_gems: {inputs: {coin: 3}, outputs: {gem: 1}}
  from_gems: {inputs: {gem: 1}, outputs: {coin: 3}}
"""
    text = text.replace("  coin:\n", "  gem: {}\n  coin:\n").replace("sword: {buy: 50c, sell: 10c", "sword: {buy: 50c, sell: 50c")
    eco = _eco(write_yaml, text)
    res = analyze_market(eco)
    assert res.prices is not None, [p.explanation for p in res.pumps]
    _verify_certificate(res)


PUMP = """
recipes:
  smelt: {inputs: {ore: 3}, outputs: {bar: 1}, cost: 2c}
"""


def test_money_pump_found_and_fix_works(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  ore: {buy: 4c, sell: 1c}\n  bar: {sell: 20c}") + PUMP
    eco = _eco(write_yaml, text)
    res = analyze_market(eco)
    assert res.prices is None and res.pumps
    p = res.pumps[0]
    assert p.kind == "money-pump" and p.net == {"coin": 6}
    assert p.capital == {"coin": 14}
    kinds = [res.market.actions[i].id for _, i in p.combo]
    assert kinds == ["buy ore", "craft smelt", "sell bar"]
    # applying any single suggested fix removes the pump
    assert "raise items.ore.buy to ≥ 6c (now 4c)" in p.fixes
    eco2 = _eco(write_yaml, text.replace("ore: {buy: 4c", "ore: {buy: 6c"))
    assert analyze_market(eco2).prices is not None
    eco3 = _eco(write_yaml, text.replace("cost: 2c", "cost: 8c"))
    assert analyze_market(eco3).prices is not None
    eco4 = _eco(write_yaml, text.replace("bar: {sell: 20c}", "bar: {sell: 14c}"))
    assert analyze_market(eco4).prices is not None


def test_buy_below_sell_and_free_item(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  amulet: {buy: 5c, sell: 6c}\n  bread: {buy: 0, consumable: true, power: 1}")
    eco = _eco(write_yaml, text)
    kinds = {p.kind for p in analyze_market(eco).pumps}
    assert {"buy-below-sell", "free-item"} <= kinds


def test_pure_duplication(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  seed: {slot: charm, power: 1}") + """
recipes:
  sprout: {inputs: {seed: 1}, outputs: {seed: 2}}
"""
    eco = _eco(write_yaml, text)
    res = analyze_market(eco)
    assert any(p.kind == "duplication" and p.net == {"seed": 1} for p in res.pumps)


def test_success_chance_expected_value_pump(write_yaml, base_yaml):
    # 2 ore (8c) -> bar (sells 20c) with 50% success: expected 10c per craft > 8c cost
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  ore: {buy: 4c}\n  bar: {sell: 20c}") + """
recipes:
  smelt: {inputs: {ore: 2}, outputs: {bar: 1}, success_chance: 0.5}
"""
    eco = _eco(write_yaml, text)
    res = analyze_market(eco)
    assert res.pumps and res.pumps[0].kind == "money-pump"
    assert res.pumps[0].net == {"coin": 4}  # 2 crafts: -16c, +1 bar expected = +20c
    mk = build_market(eco)
    craft = next(a for a in mk.actions if a.kind == "craft")
    assert craft.int_delta() == (2, {"ore": -4, "bar": 1})


def test_unprotected_junk_is_not_a_pump(write_yaml, base_yaml):
    text = base_yaml.replace("  gel: {sell: 2c}", "  gel: {sell: 2c}\n  pebble: {buy: 0}")
    eco = _eco(write_yaml, text)
    res = analyze_market(eco)
    assert "pebble" not in res.market.protected
    assert res.prices is not None


def test_fixed_level_lifespan_is_one_over_q(write_yaml, base_yaml):
    eco = _eco(write_yaml, base_yaml)
    loc = eco.locations["meadow"]
    st = encounter_stats(eco, loc, 2, 1.0)
    q = sum(v.prob * (1 - eco.odds(3.0, v.level)) for v in loc.variants)
    assert st.death_prob == pytest.approx(q)
    assert st.lifespan == pytest.approx(1 / q)


def test_spawn_study_matches_monte_carlo(write_yaml, base_yaml):
    eco = _eco(write_yaml, base_yaml)
    loc = eco.locations["meadow"]
    s = spawn_study(eco, loc, 1, "geared")
    rng = random.Random(7)
    lives, levels = [], []
    for _ in range(20000):
        lvl, xp, n = 1, 0, 0
        while True:
            n += 1
            pw = loadout(eco, lvl, "geared").power
            v = rng.choices(loc.variants, [v.prob for v in loc.variants])[0]
            if rng.random() >= eco.odds(lvl + pw, v.level):
                break
            xp += eco.xp_gain(eco.enemies[v.enemy].xp, lvl, v.level)
            while lvl < eco.max_level and xp >= eco.xp_to_next(lvl):
                xp -= eco.xp_to_next(lvl)
                lvl += 1
        lives.append(n)
        levels.append(lvl)
    mean = sum(lives) / len(lives)
    sd = (sum((x - mean) ** 2 for x in lives) / len(lives)) ** 0.5
    assert abs(s.lifespan - mean) < 4 * sd / len(lives) ** 0.5
    assert s.death_level == pytest.approx(sum(levels) / len(levels), abs=0.05)


def test_immortal_threshold(write_yaml, base_yaml):
    eco = _eco(write_yaml, base_yaml.replace("0.01, 0.99)", "0, 1)"))
    st = encounter_stats(eco, eco.locations["meadow"], 5, 50.0)
    assert st.death_prob == 0 and st.lifespan == float("inf")


def test_death_farming_profit(write_yaml, base_yaml):
    from econ.analysis.respawn import death_profit
    text = base_yaml + """
respawn:
  currencies: {coin: floor(amount * 0.9)}
  grants:
    - {item: sword, if_missing: true}
    - {currency: coin, amount: 5, if_missing: true}
"""
    eco = _eco(write_yaml, text)
    # broke: topped up to 5c, plus a sword that sells for 10c
    assert death_profit(eco, "coin", 0) == 15
    # holding 100c: lose 10c, no top-up, +10c sword
    assert death_profit(eco, "coin", 100) == 0
    # holding 50c: lose 5c, +10c sword
    assert death_profit(eco, "coin", 50) == 5
