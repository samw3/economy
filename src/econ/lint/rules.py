"""Lint rules: turn the analyses into findings (with evidence and fixes) and metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..analysis.combat import (
    EncounterStats, Loadout, currency_sources, encounter_stats, intended_locations, item_sources,
    level_coverage_gaps, loadout, replacement_cost, spawn_study, upkeep_per_encounter,
)
from ..analysis.market import MarketResult, analyze_market
from ..analysis.respawn import death_farming, death_loss, penalty_evasion
from ..economy import Economy, Location
from ..formula import FormulaError
from .findings import RULES, Finding


def _in(v: float, rng) -> bool:
    if rng is None:
        return True
    lo, hi = rng
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def _fmt_range(rng, pct: bool = False) -> str:
    lo, hi = rng
    f = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v:g}")
    if lo is None:
        return f"≤ {f(hi)}"
    if hi is None:
        return f"≥ {f(lo)}"
    return f"{f(lo)}–{f(hi)}"


def _runs(levels: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for l in sorted(levels):
        if out and l == out[-1][1] + 1:
            out[-1] = (out[-1][0], l)
        else:
            out.append((l, l))
    return out


def _lv(r: tuple[int, int]) -> str:
    return f"level {r[0]}" if r[0] == r[1] else f"levels {r[0]}–{r[1]}"


def _life(x: float) -> str:
    if math.isinf(x):
        return "∞"
    return f"{x:,.0f}" if x >= 10 else f"{x:.1f}"


@dataclass
class Ctx:
    eco: Economy
    findings: list[Finding] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    market: MarketResult | None = None

    def __post_init__(self):
        self.sources = item_sources(self.eco)
        self.obtainable = {i for i, s in self.sources.items() if s}
        self.cur_sources = currency_sources(self.eco)
        self._stats: dict = {}
        self._loadouts: dict = {}

    def add(self, code: str, path: str, message: str, *, detail: str = "", fix: str = "",
            severity: str | None = None, data: dict | None = None) -> Finding:
        sev = severity or RULES[code].severity
        file, line = self.eco.locate(path) if path else (None, None)
        f = Finding(code, sev, message, path=path, detail=detail, fix=fix, file=file, line=line, data=data or {})
        self.findings.append(f)
        return f

    def loadout(self, level: int, kind: str) -> Loadout:
        key = (level, kind)
        if key not in self._loadouts:
            self._loadouts[key] = loadout(self.eco, level, kind, self.obtainable)
        return self._loadouts[key]

    def stats(self, loc: Location, level: int, kind: str, extra_power: float = 0.0) -> EncounterStats:
        key = (loc.id, level, kind, extra_power)
        if key not in self._stats:
            self._stats[key] = encounter_stats(self.eco, loc, level, self.loadout(level, kind).power + extra_power)
        return self._stats[key]

    def primary_income(self, st: EncounterStats) -> float:
        return st.income.get(self.eco.primary, 0.0)


# ----------------------------------------------------------------------------------------
# structure & items

def check_structure(ctx: Ctx):
    eco = ctx.eco
    placed = {v.enemy for l in eco.locations.values() if not l.safe for v in l.variants}
    for eid in eco.enemies:
        if eid not in placed:
            ctx.add("unused", f"enemies.{eid}", f"enemy '{eid}' does not appear in any location",
                    fix="Add it to a location's `enemies` list or delete it.")
    recipe_inputs = set()
    for r in eco.recipes.values():
        recipe_inputs.update(g for g in r.inputs if g in eco.items)
        missing = [g for g in r.inputs if g in eco.items and g not in ctx.obtainable]
        missing += [c for c in list(r.inputs) + list(r.cost) if c in eco.currencies and not ctx.cur_sources.get(c)]
        if missing:
            ctx.add("unused", f"recipes.{r.id}", f"recipe '{r.id}' can never be used: {', '.join(missing)} cannot be obtained")
    for iid, it in eco.items.items():
        if iid not in ctx.obtainable:
            why = " (its buy price exceeds the wallet cap)" if it.buy is not None else ""
            ctx.add("unobtainable-item", f"items.{iid}", f"item '{iid}' can never be obtained{why}",
                    fix="Give it an affordable buy price, add it to an enemy's drops, or make a recipe produce it.")
            continue
        sellable = bool(it.sell) and any(v > 0 for v in it.sell.values())
        if not sellable and not it.slot and not it.consumable and it.power == 0 and iid not in recipe_inputs:
            ctx.add("dead-item", f"items.{iid}", f"item '{iid}' has no use (cannot be sold, equipped, consumed or crafted with)",
                    fix="Give it a `sell` price or use it as a recipe input.")
    # currencies
    for cid, cur in eco.currencies.items():
        srcs = ctx.cur_sources.get(cid, [])
        if not srcs:
            ctx.add("no-source", f"currencies.{cid}", f"currency '{cid}' can never be obtained")
            continue
        sinks = []
        for it in eco.items.values():
            if it.buy and it.buy.get(cid):
                sinks.append(f"buy {it.id}")
        for r in eco.recipes.values():
            if r.cost.get(cid) or r.inputs.get(cid):
                sinks.append(f"recipe {r.id}")
        f = eco.respawn.currencies.get(cid)
        if f is not None:
            try:
                if death_loss(eco, cid, 1000) > 0:
                    sinks.append("death penalty")
            except FormulaError:
                pass
        if not sinks:
            ctx.add("no-sink", f"currencies.{cid}",
                    f"currency '{cid}' can be earned ({', '.join(srcs[:3])}) but never spent",
                    fix="Price some items or recipe costs in it, or add a respawn penalty.")
        if cur.cap is not None:
            for it in eco.items.values():
                if it.buy and it.buy.get(cid, 0) > cur.cap:
                    ctx.add("unaffordable", f"items.{it.id}.buy",
                            f"'{it.id}' costs {cur.format(it.buy[cid])} but the {cid} wallet holds at most {cur.format(cur.cap)}",
                            fix=f"Raise currencies.{cid}.wallet_cap or lower the price.")
            for r in eco.recipes.values():
                need = r.cost.get(cid, 0) + r.inputs.get(cid, 0)
                if need > cur.cap:
                    ctx.add("unaffordable", f"recipes.{r.id}",
                            f"recipe '{r.id}' needs {cur.format(need)} but the {cid} wallet holds at most {cur.format(cur.cap)}")
            if eco.start.currencies.get(cid, 0) > cur.cap:
                ctx.add("bad-value", "player.start.currencies",
                        f"start {cid} {cur.format(eco.start.currencies[cid])} exceeds wallet cap {cur.format(cur.cap)}",
                        severity="warning")
    # dominated gear
    for slot, items in eco.gear_slots().items():
        for b in items:
            if b.buy is None:
                continue
            for a in items:
                if a is b or a.buy is None or set(a.buy) != set(b.buy):
                    continue
                if (a.power >= b.power and a.min_level <= b.min_level and all(a.buy[c] <= b.buy[c] for c in a.buy)
                        and a.break_chance <= b.break_chance
                        and (a.power > b.power or a.min_level < b.min_level or any(a.buy[c] < b.buy[c] for c in a.buy)
                             or a.break_chance < b.break_chance)):
                    ctx.add("dominated-gear", f"items.{b.id}",
                            f"'{b.id}' is dominated by '{a.id}' ({slot}: power {a.power:g} ≥ {b.power:g}, "
                            f"price {eco.fmt(a.buy)} ≤ {eco.fmt(b.buy)}, level {a.min_level} ≤ {b.min_level})")
                    break
    # crafting that is never worth it
    for r in eco.recipes.values():
        outs = [g for g in r.outputs if g in eco.items]
        if len(outs) != 1 or len(r.outputs) != 1:
            continue
        out = eco.items[outs[0]]
        if out.buy is None or set(out.buy) != {eco.primary}:
            continue
        cost = r.cost.get(eco.primary, 0)
        ok = True
        for g, q in r.inputs.items():
            if g == eco.primary:
                cost += q
            elif g in eco.items and eco.items[g].buy is not None and set(eco.items[g].buy) == {eco.primary}:
                cost += q * eco.items[g].buy[eco.primary]
            else:
                ok = False  # inputs come from loot: crafting may still be worth it
        if ok and cost / float(r.success) > out.buy[eco.primary] * r.outputs[out.id]:
            ctx.add("crafting-unprofitable", f"recipes.{r.id}",
                    f"crafting '{out.id}' via '{r.id}' costs {eco.fmt_amount(eco.primary, cost / float(r.success))} "
                    f"but it can be bought for {eco.fmt(out.buy)}")


# ----------------------------------------------------------------------------------------
# battle function

def check_battle(ctx: Ctx):
    eco = ctx.eco
    max_gear = max((ctx.loadout(l, "geared").power for l in range(1, eco.max_level + 1)), default=0.0)
    max_cons = max((i.power for i in eco.items.values() if i.consumable), default=0.0)
    enemy_levels = sorted({v.level for l in eco.locations.values() for v in l.variants} | {e.level for e in eco.enemies.values()})
    top = eco.max_level + max_gear + max_cons
    effs = [1 + 0.5 * k for k in range(int((top - 1) / 0.5) + 2)]
    worst = None
    try:
        grid = {(p, e): eco.raw_odds(p, e) for p in effs for e in enemy_levels}
    except FormulaError as err:
        ctx.add("bad-formula", "battle.formula", str(err))
        return
    for (p, e), v in grid.items():
        if v < -1e-9 or v > 1 + 1e-9:
            if worst is None or abs(v - 0.5) > abs(worst[2] - 0.5):
                worst = (p, e, v)
    if worst:
        ctx.add("odds-range", "battle.formula",
                f"battle formula returns {worst[2]:.4g} at playerLevel={worst[0]:g}, enemyLevel={worst[1]} (must be in [0, 1])",
                fix="Wrap it in clamp(..., 0, 1) or fix its scale.")
    # monotonicity
    bad = None
    for e in enemy_levels:
        for a, b in zip(effs, effs[1:]):
            if grid[(b, e)] < grid[(a, e)] - 1e-12:
                bad = f"odds drop from {grid[(a, e)]:.4f} to {grid[(b, e)]:.4f} when playerLevel rises {a:g}→{b:g} (enemyLevel {e})"
                break
        if bad:
            break
    if not bad:
        for p in effs:
            for a, b in zip(enemy_levels, enemy_levels[1:]):
                if grid[(p, b)] > grid[(p, a)] + 1e-12:
                    bad = f"odds rise from {grid[(p, a)]:.4f} to {grid[(p, b)]:.4f} when enemyLevel rises {a}→{b} (playerLevel {p:g})"
                    break
            if bad:
                break
    if bad:
        ctx.add("odds-not-monotone", "battle.formula", f"battle formula is not monotone: {bad}")
    ctx.metrics["odds_monotone"] = bad is None
    # even fights
    even = [(l, eco.odds(l, l)) for l in range(1, eco.max_level + 1)]
    lo = min(v for _, v in even)
    hi = max(v for _, v in even)
    ctx.metrics["even_fight_win"] = {"min": round(lo, 4), "max": round(hi, 4)}
    tgt = eco.targets.even_fight_win
    if tgt is not None and (not _in(lo, tgt) or not _in(hi, tgt)):
        ctx.add("even-fight-odds", "battle.formula",
                f"even-fight win chance is {lo:.1%}" + (f"–{hi:.1%}" if hi - lo > 5e-4 else "")
                + f" (target {_fmt_range(tgt, pct=True)})",
                fix="Shift the formula (e.g. a bias parameter) so that playerLevel == enemyLevel lands in the target.")
    # odds table for the report
    sample = sorted({1, max(1, eco.max_level // 2), eco.max_level})
    ctx.metrics["odds_table"] = {
        f"delta {d:+d}": round(eco.odds(sample[len(sample) // 2] + d, sample[len(sample) // 2]), 4)
        for d in (-4, -2, -1, 0, 1, 2, 4)
    }


# ----------------------------------------------------------------------------------------
# market

def check_market(ctx: Ctx):
    eco = ctx.eco
    res = analyze_market(eco)
    ctx.market = res
    m = {"goods": len(res.market.goods), "actions": len(res.market.actions),
         "protected_goods": sorted(res.market.protected)}
    if res.prices is not None:
        m["status"] = "no pump (shadow-price certificate found)"
        m["shadow_prices"] = {g: round(res.shadow_price(g, eco), 4) for g in res.market.goods
                              if res.shadow_price(g, eco) is not None}
    elif res.pumps:
        m["status"] = f"{len(res.pumps)} pump(s) found"
    else:
        m["status"] = "inconclusive"
    ctx.metrics["market"] = m
    for k, p in enumerate(res.pumps):
        acts = [res.market.actions[i] for _, i in p.combo]
        anchor = next((a for a in acts if a.kind == "craft"), None) or next((a for a in acts if a.kind == "sell"), acts[0])
        gain = ", ".join(eco.fmt_good(g, v) for g, v in p.net.items() if v > 0)
        if p.kind == "buy-below-sell":
            it = eco.items[acts[0].ref]
            msg = f"'{it.id}' can be bought for {eco.fmt(it.buy)} and sold for {eco.fmt(it.sell)}"
            path = f"items.{it.id}.sell"
            code = "buy-below-sell"
        elif p.kind == "free-item":
            msg = f"free {gain}: " + ", ".join(a.id for a in acts) + " costs nothing"
            path = anchor.path
            code = "free-item"
        elif p.kind == "duplication":
            msg = f"loop duplicates items (+{gain} per loop): " + " → ".join(a.id for a in acts)
            path = anchor.path
            code = "duplication"
        else:
            msg = f"money pump (+{gain} per loop): " + " → ".join(a.id for a in acts)
            path = anchor.path
            code = "money-pump"
        f = ctx.add(code, path, msg, detail="\n".join(p.explanation),
                    fix=("Any one of: " + "; ".join(p.fixes)) if p.fixes else "",
                    data={"pump_index": k, "net": p.net, "capital": p.capital,
                          "actions": [{"times": t, "action": res.market.actions[i].id} for t, i in p.combo]})
        if code == "free-item":
            f.severity = "error" if any(g in res.market.protected for g in p.net) else "warning"
    if res.inconclusive:
        ctx.add("money-pump", "recipes", f"market analysis inconclusive: {res.inconclusive}", severity="warning")
    from ..analysis.market import grind_bounds, income_ceiling
    gb = grind_bounds(eco)
    ctx.metrics["fastest"] = {b.item: {"events": b.events}
                              for b in sorted(gb.values(), key=lambda b: -b.events) if b.events >= 2}
    ic = income_ceiling(eco)
    m["income_ceiling_per_event"] = float(ic.D) if ic is not None else None


# ----------------------------------------------------------------------------------------
# lifespan & difficulty

def check_lifespan(ctx: Ctx):
    eco = ctx.eco
    tgt = eco.targets.lifespan
    table = []
    for loc in eco.location_order():
        if not loc.level_range:
            continue
        a, b = loc.level_range
        b = min(b, eco.max_level)
        rows = []
        short, long_, immortal, hopeless = [], [], [], []
        for lvl in range(a, b + 1):
            n = ctx.stats(loc, lvl, "naked")
            g = ctx.stats(loc, lvl, "geared")
            row = {"level": lvl, "death_prob_naked": round(n.death_prob, 5), "death_prob_geared": round(g.death_prob, 5),
                   "lifespan_naked": n.lifespan, "lifespan_geared": g.lifespan, "gear": ctx.loadout(lvl, "geared").describe(),
                   "xp_per_encounter": round(g.xp, 3), "income_per_encounter": round(ctx.primary_income(g), 3),
                   "hardest_win_geared": round(g.hardest_win, 4)}
            rows.append(row)
            if tgt is not None:
                lo, hi = tgt
                if lo is not None and g.lifespan < lo:
                    short.append(lvl)
                if hi is not None and n.lifespan > hi:
                    long_.append(lvl)
            if g.death_prob <= 0:
                immortal.append(lvl)
            if g.hardest_win <= 0:
                hopeless.append(lvl)
        spawn = {}
        for kind in ("naked", "geared"):
            try:
                s = spawn_study(eco, loc, a, kind, ctx.obtainable)
                spawn[kind] = {"lifespan": s.lifespan, "death_level": round(s.death_level, 2), "note": s.note}
            except Exception as e:  # pragma: no cover - numerical edge cases
                spawn[kind] = {"lifespan": None, "note": f"failed: {e}"}
        table.append({"location": loc.id, "level_range": [a, b], "rows": rows, "spawn": spawn,
                      "encounter_rate": loc.rate})
        path = f"locations.{loc.id}"
        if short:
            lifes = [r["lifespan_geared"] for r in rows if r["level"] in short]
            rr = _runs(short)
            sp = spawn.get("geared", {}).get("lifespan")
            ctx.add("lifespan-short", path,
                    f"{loc.id} is too hard at {', '.join(_lv(r) for r in rr)}: expected lifespan "
                    f"{_life(min(lifes))}" + (f"–{_life(max(lifes))}" if len(lifes) > 1 else "")
                    + f" encounters even with the best gear (target ≥ {tgt[0]:g})",
                    detail=_life_table(rows, loc) + (f"\nspawn study (geared, levelling up while staying here from level {a}): "
                                                      f"{_life(sp)} encounters" if sp is not None else ""),
                    fix=f"Lower enemy levels in {loc.id}, raise its level_range, make gear stronger, or make the battle formula more forgiving.",
                    data={"levels": short, "lifespans": lifes})
        if long_:
            lifes = [r["lifespan_naked"] for r in rows if r["level"] in long_]
            rr = _runs(long_)
            ctx.add("lifespan-long", path,
                    f"{loc.id} is too easy at {', '.join(_lv(r) for r in rr)}: even without gear players live "
                    f"{_life(min(lifes))}" + (f"–{_life(max(lifes))}" if len(lifes) > 1 else "")
                    + f" encounters (target ≤ {tgt[1]:g})",
                    detail=_life_table(rows, loc),
                    fix=f"Add stronger enemies to {loc.id}, lower its level_range, or lower the battle formula's ceiling.",
                    data={"levels": long_, "lifespans": lifes})
        # the levelling spawn study is the purest "lifespan of a single spawn"
        if tgt is not None:
            sg = spawn.get("geared", {}).get("lifespan")
            sn = spawn.get("naked", {}).get("lifespan")
            if not short and tgt[0] is not None and sg is not None and sg < tgt[0]:
                ctx.add("lifespan-short", path,
                        f"{loc.id} is too hard: a player spawning at level {a} with the best gear lives {_life(sg)} "
                        f"encounters on average, even counting level-ups (target ≥ {tgt[0]:g})",
                        detail=_life_table(rows, loc),
                        fix=f"Lower enemy levels in {loc.id} or make the battle formula more forgiving.",
                        data={"spawn_lifespan": sg})
            if (not long_ and not immortal and tgt[1] is not None and sn is not None and sn > tgt[1]):
                ctx.add("lifespan-long", path,
                        f"{loc.id} is too easy: a player spawning at level {a} without gear lives {_life(sn)} "
                        f"encounters on average (levelling up as they go; target ≤ {tgt[1]:g})",
                        detail=_life_table(rows, loc),
                        fix=f"Add stronger enemies to {loc.id} or lower the battle formula's ceiling.",
                        data={"spawn_lifespan": sn})
        if immortal:
            ctx.add("immortal", path, f"players can never die in {loc.id} at {', '.join(_lv(r) for r in _runs(immortal))} "
                    "(win probability is 1 against every enemy)",
                    fix="Cap the battle formula below 1, e.g. clamp(..., 0.01, 0.99).")
        if hopeless:
            ctx.add("hopeless", path, f"some enemies in {loc.id} can never be beaten at {', '.join(_lv(r) for r in _runs(hopeless))}",
                    fix="Give underdogs a floor, e.g. clamp(..., 0.01, 0.99), or lower those enemies' levels.")
    ctx.metrics["locations"] = table


def _life_table(rows: list[dict], loc: Location) -> str:
    out = ["level  death%(geared)  lifespan naked / geared  gear"]
    for r in rows:
        out.append(f"{r['level']:>5}  {r['death_prob_geared']:>13.2%}  {_life(r['lifespan_naked']):>13} / "
                   f"{_life(r['lifespan_geared']):<8}  {r['gear']}")
    return "\n".join(out)


# ----------------------------------------------------------------------------------------
# progression

def check_progression(ctx: Ctx):
    eco = ctx.eco
    gaps = level_coverage_gaps(eco)
    for g in gaps:
        ctx.add("level-gap", "locations", f"no location is designed for {_lv(g)} (no level_range covers it)",
                fix="Extend a neighbouring location's level_range or add a location for these levels.")
    tgt = eco.targets.encounters_per_level
    pace = []
    fast, slow = [], []
    total = 0.0
    minutes = 0.0
    uncovered = []
    for lvl in range(1, eco.max_level):
        locs = intended_locations(eco, lvl)
        if not locs:
            pace.append({"level": lvl, "location": None, "encounters": None})
            uncovered.append(lvl)
            continue
        best = max(locs, key=lambda l: ctx.stats(l, lvl, "geared").xp)
        st = ctx.stats(best, lvl, "geared")
        need = eco.xp_to_next(lvl)
        enc = math.inf if st.xp <= 0 else need / st.xp
        total += enc
        minutes += enc / best.rate
        pace.append({"level": lvl, "location": best.id, "xp_to_next": need, "xp_per_encounter": round(st.xp, 3),
                     "encounters": enc})
        if tgt is not None:
            lo, hi = tgt
            if lo is not None and enc < lo:
                fast.append((lvl, enc, st.xp, need, best.id))
            if hi is not None and enc > hi:
                slow.append((lvl, enc, st.xp, need, best.id))
    # play time uses each location's own encounter rate; unknown if some level has no location
    hours = minutes / 60 if (math.isfinite(minutes) and not uncovered and eco.max_level > 1) else None
    ctx.metrics["leveling"] = {"per_level": pace, "encounters_to_max": total if not uncovered else None,
                               "hours_to_max": hours, "uncovered_levels": uncovered}
    ht = eco.targets.hours_to_max_level
    if ht is not None and hours is not None and hours > 0 and not _in(hours, ht):
        word = "fast" if ht[0] is not None and hours < ht[0] else "slow"
        target_h = ht[0] if word == "fast" else ht[1]
        ctx.add("leveling-pace", "targets.hours_to_max_level",
                f"reaching level {eco.max_level} takes ~{hours:.1f} h ({total:,.0f} encounters) "
                f"at the intended locations — too {word} (target {_fmt_range(ht)} h)",
                fix=f"Scale progression.xp_to_next by ~{target_h / hours:.2f}× (or enemy xp by the inverse).")
    for group, word in ((fast, "fast"), (slow, "slow")):
        if not group:
            continue
        rr = _runs([g[0] for g in group])
        lo, hi = tgt
        detail = "\n".join(f"level {l}: {e:.1f} encounters ({need} XP at {x:.2f} XP/encounter in {loc})"
                           for l, e, x, need, loc in group)
        if word == "fast":
            k = max(lo / e for _, e, _, _, _ in group)
            fix = (f"Multiply progression.xp_to_next by ≥ {math.ceil(k * 10) / 10:g} at {', '.join(_lv(r) for r in rr)} "
                   "(or lower enemy xp), e.g. " + "; ".join(
                       f"level {l} needs ≥ {math.ceil(lo * x)} XP (now {need})" for l, e, x, need, loc in group[:3]))
        else:
            k = min(hi / e for _, e, _, _, _ in group)
            fix = (f"Multiply progression.xp_to_next by ≤ {math.floor(k * 100) / 100:g} at {', '.join(_lv(r) for r in rr)} "
                   "(or raise enemy xp), e.g. " + "; ".join(
                       f"level {l} needs ≤ {math.floor(hi * x)} XP (now {need})" for l, e, x, need, loc in group[:3]))
        ctx.add("leveling-pace", "progression.xp_to_next",
                f"levelling is too {word} at {', '.join(_lv(r) for r in rr)}: "
                f"{min(g[1] for g in group):.1f}–{max(g[1] for g in group):.1f} encounters per level (target {_fmt_range(tgt)})",
                detail=detail, fix=fix)
    _risk_reward(ctx)


def _risk_reward(ctx: Ctx):
    eco = ctx.eco
    locs = [l for l in eco.location_order() if l.level_range]
    inversions: dict[tuple[str, str, str], list[int]] = {}
    for lvl in range(1, eco.max_level + 1):
        intended = intended_locations(eco, lvl)
        if not intended:
            continue
        best = max(intended, key=lambda l: ctx.stats(l, lvl, "geared").xp)
        sb = ctx.stats(best, lvl, "geared")
        for j in locs:
            if j in intended or j.level_range[1] >= lvl:
                continue
            sj = ctx.stats(j, lvl, "geared")
            if sj.death_prob > sb.death_prob + 1e-12:
                continue
            more_xp = sj.xp > 1.05 * sb.xp
            more_inc = ctx.primary_income(sj) > 1.05 * ctx.primary_income(sb)
            if more_xp and more_inc:
                kind = "both"
            elif more_xp:
                kind = "xp"
            elif more_inc:
                kind = "income"
            else:
                continue
            inversions.setdefault((j.id, best.id, kind), []).append(lvl)
    for (jid, iid, kind), lvls in inversions.items():
        j, i = eco.locations[jid], eco.locations[iid]
        l0 = lvls[0]
        sj, si = ctx.stats(j, l0, "geared"), ctx.stats(i, l0, "geared")
        what = {"both": "more XP and more income", "xp": "more XP", "income": "more income"}[kind]
        ctx.add("risk-reward-inversion", f"locations.{jid}",
                f"at {', '.join(_lv(r) for r in _runs(lvls))}, easier '{jid}' gives {what} than '{iid}' "
                f"(the location designed for those levels) at no extra risk",
                detail=f"at level {l0}: {jid}: death {sj.death_prob:.2%}, {sj.xp:.1f} XP, "
                       f"{eco.fmt_amount(eco.primary, ctx.primary_income(sj))}/encounter; "
                       f"{iid}: death {si.death_prob:.2%}, {si.xp:.1f} XP, {eco.fmt_amount(eco.primary, ctx.primary_income(si))}/encounter",
                fix=(f"Reduce XP for over-levelled players via progression.xp_gain (e.g. scale by enemyLevel - playerLevel), "
                     f"or raise rewards in {iid}." if kind != "income" else
                     f"Raise drops in {iid} or lower drops in {jid}."),
                severity="warning" if kind != "income" else "info")
    # dominated locations
    for j in locs:
        for k in locs:
            if j is k:
                continue
            a, b = j.level_range
            levels = range(a, min(b, eco.max_level) + 1)
            dom = True
            strict = False
            for lvl in levels:
                sj, sk = ctx.stats(j, lvl, "geared"), ctx.stats(k, lvl, "geared")
                if not (sk.death_prob <= sj.death_prob + 1e-12 and sk.xp >= sj.xp - 1e-9
                        and ctx.primary_income(sk) >= ctx.primary_income(sj) - 1e-9):
                    dom = False
                    break
                if sk.death_prob < sj.death_prob - 1e-9 or sk.xp > sj.xp + 1e-9 or ctx.primary_income(sk) > ctx.primary_income(sj) + 1e-9:
                    strict = True
            if dom and strict:
                ctx.add("location-dominated", f"locations.{j.id}",
                        f"'{k.id}' is at least as safe and gives at least as much XP and income as '{j.id}' at every level in {j.id}'s range")
                break


# ----------------------------------------------------------------------------------------
# income, upkeep, wallet

def check_income(ctx: Ctx):
    eco = ctx.eco
    # wallet overflow
    wf = eco.targets.wallet_fill_encounters
    for cid, cur in eco.currencies.items():
        if cur.cap is None:
            continue
        best = None
        for loc in eco.location_order():
            if not loc.level_range:
                continue
            lvl = min(loc.level_range[1], eco.max_level)
            inc = ctx.stats(loc, lvl, "geared").income.get(cid, 0.0)
            if best is None or inc > best[0]:
                best = (inc, loc.id, lvl)
        for e in eco.enemies.values():
            for d in e.drops:
                if d.kind == "currency" and d.id == cid and d.qmax > cur.cap:
                    ctx.add("wallet-overflow", f"enemies.{e.id}.drops",
                            f"'{e.id}' can drop {cur.format(d.qmax)}, more than the wallet cap {cur.format(cur.cap)}")
        ctx.metrics.setdefault("wallet", {})[cid] = {
            "cap": cur.cap,
            "fill_encounters": round(cur.cap / best[0], 1) if best and best[0] > 0 else None,
            "best_income_location": best[1] if best else None,
        }
        if best and best[0] > 0 and wf is not None:
            fill = cur.cap / best[0]
            if fill < wf:
                ctx.add("wallet-overflow", f"currencies.{cid}.wallet_cap",
                        f"the {cid} wallet ({cur.format(cur.cap)}) fills after ~{fill:.0f} encounters at {best[1]} "
                        f"(level {best[2]}, {cur.format(best[0])}/encounter); target ≥ {wf:g}",
                        fix=f"Raise wallet_cap to ≥ {cur.format(math.ceil(best[0] * wf))} or add sinks.")
    # upkeep
    ratio = eco.targets.max_upkeep_ratio
    rows = []
    heavy = []
    for lvl in range(1, eco.max_level + 1):
        lo = ctx.loadout(lvl, "geared")
        up = upkeep_per_encounter(eco, lo)
        intended = intended_locations(eco, lvl)
        if not intended:
            continue
        inc = max(ctx.primary_income(ctx.stats(l, lvl, "geared")) for l in intended)
        rows.append({"level": lvl, "upkeep": round(up, 3), "income": round(inc, 3), "loadout": lo.describe()})
        if ratio is not None and inc > 0 and up > ratio * inc:
            heavy.append((lvl, up, inc, lo))
        elif ratio is not None and inc <= 0 and up > 0:
            heavy.append((lvl, up, inc, lo))
    ctx.metrics["upkeep"] = rows
    if heavy:
        rr = _runs([h[0] for h in heavy])
        worst = max(heavy, key=lambda h: h[1] / max(h[2], 1e-9))
        ctx.add("upkeep-heavy", "items",
                f"gear upkeep is {worst[1]:.2f} per encounter vs income {worst[2]:.2f} at level {worst[0]} "
                f"(> {ratio:.0%} of income) — affects {', '.join(_lv(r) for r in rr)}",
                detail="\n".join(f"level {l}: upkeep {u:.2f} vs income {i:.2f} ({lo.describe()})" for l, u, i, lo in heavy[:8]),
                fix="Increase gear half_life, lower gear prices, or raise income at those levels.")
    _consumable_abuse(ctx)


def _consumable_abuse(ctx: Ctx):
    eco = ctx.eco
    cons = [i for i in eco.items.values() if i.consumable and i.power > 0 and i.buy is not None
            and set(i.buy) == {eco.primary}]
    risk_cap = eco.policy.max_death_risk
    for c in cons:
        cost = c.buy[eco.primary] * c.break_chance
        hits = []
        for lvl in range(1, eco.max_level + 1):
            if c.min_level > lvl:
                continue
            intended = intended_locations(eco, lvl)
            if not intended:
                continue
            base = max(intended, key=lambda l: ctx.primary_income(ctx.stats(l, lvl, "geared")))
            sb = ctx.stats(base, lvl, "geared")
            for j in eco.location_order():
                if not j.level_range or j.level_range[0] <= lvl:
                    continue
                sj = ctx.stats(j, lvl, "geared", extra_power=c.power)
                net = ctx.primary_income(sj) - cost
                if sj.death_prob <= max(risk_cap, sb.death_prob) and net > 1.5 * ctx.primary_income(sb) and net > 0:
                    hits.append((lvl, j.id, sj.death_prob, net, ctx.primary_income(sb), base.id))
        if hits:
            l0 = hits[0]
            ctx.add("consumable-abuse", f"items.{c.id}",
                    f"with '{c.id}' (+{c.power:g} power, {eco.fmt(c.buy)}), level-{l0[0]} players can farm '{l0[1]}' "
                    f"at {l0[2]:.1%} death risk for {eco.fmt_amount(eco.primary, l0[3])}/encounter net of potions, "
                    f"vs {eco.fmt_amount(eco.primary, l0[4])} at '{l0[5]}'",
                    detail="\n".join(f"level {l}: {j} death {q:.1%}, net {eco.fmt_amount(eco.primary, n)} vs {eco.fmt_amount(eco.primary, b)} at {bl}"
                                     for l, j, q, n, b, bl in hits[:8]),
                    fix=f"Raise items.{c.id}.buy, lower its power, or reduce drops of the enemies in {l0[1]}.")


# ----------------------------------------------------------------------------------------
# respawn

def check_respawn(ctx: Ctx):
    eco = ctx.eco
    tgt = eco.targets.lifespan
    locs = [l for l in eco.location_order()]
    if not locs:
        return
    # death farming: a broke level-1 player, as dangerous a location as they like
    naked1 = [encounter_stats(eco, l, 1, 0.0) for l in locs]
    best_inc = max(ctx.primary_income(s) for s in naked1)
    max_q = max(s.death_prob for s in naked1)
    df = death_farming(eco, best_inc, max_q)
    if df:
        cur = eco.currencies[df.currency]
        sev = "error" if df.farm_rate >= 0.5 * df.normal_rate else "warning"
        what = (f"respawn grants {eco.fmt(df.grant)} of sellable value ({', '.join(df.grant_items) or 'currency'}) "
                "while the death penalty takes less") if df.grant else "the respawn formulas leave them with more money than before"
        ctx.add("death-farming", "respawn.grants" if df.grant else f"respawn.currencies.{df.currency}",
                f"dying is profitable for players holding less than {cur.format(df.profitable_below)}: {what}",
                detail=(f"a broke player gains {cur.format(df.profit_when_broke)} per death; dying takes ~{1 / max(df.deaths_per_encounter, 1e-9):.1f} "
                        f"encounters at the deadliest location → {cur.format(df.farm_rate)}/encounter vs "
                        f"{cur.format(df.normal_rate)}/encounter from normal play at level 1"
                        + (f"\n{df.xp_cost}" if df.xp_cost else "")),
                fix="Remove `sell` prices from granted items, grant less, or add a flat death fee "
                    "(e.g. respawn.currencies: max(0, floor(amount * 0.85) - 10)).",
                severity=sev,
                data={"profitable_below": df.profitable_below, "profit_when_broke": df.profit_when_broke})
    pe = penalty_evasion(eco)
    if pe:
        cur = eco.currencies[pe.currency]
        ctx.add("penalty-evasion", f"items.{pe.item}",
                f"death costs {pe.penalty_rate:.0%} of {pe.currency} (at {cur.format(pe.reference_amount)}) but buying "
                f"'{pe.item}' and selling it back only loses {pe.spread:.0%}"
                + (f" (+{pe.item_loss:.0%} chance to lose it on death)" if pe.item_loss else "")
                + ": players can park their money in it",
                fix=f"Widen the buy/sell spread of {pe.item}, apply inventory losses on death, or lower the currency penalty.")
    # death spiral
    r = eco.respawn
    if tgt is not None and tgt[0] is not None and (r.eq_lose >= 0.5 or not r.is_identity_level()):
        spiral = []
        for lvl in range(1, eco.max_level + 1):
            try:
                new_level = int(math.floor(r.level.eval({"level": float(lvl), "xp": 0.0, "deaths": 1.0}) + 1e-9))
            except FormulaError:
                break
            new_level = min(max(1, new_level), eco.max_level)
            granted = {g.id for g in r.grants if g.kind == "item"}
            if r.eq_lose >= 0.5:
                pool = granted & set(eco.items)
                lo = loadout(eco, new_level, "geared", pool)
            else:
                lo = ctx.loadout(new_level, "geared")
            best = max(encounter_stats(eco, l, new_level, lo.power).lifespan for l in locs)
            if best < tgt[0]:
                spiral.append((lvl, new_level, best, lo))
        if spiral:
            l0 = spiral[0]
            ctx.add("death-spiral", "respawn",
                    f"after dying at {', '.join(_lv(x) for x in _runs([s[0] for s in spiral]))} players respawn so weak that their "
                    f"best expected lifespan anywhere is {_life(l0[2])} encounters (target ≥ {tgt[0]:g})",
                    detail="\n".join(f"died at level {a} → respawn at level {b} with {lo.describe()}: best lifespan {_life(life)}"
                                     for a, b, life, lo in spiral[:8]),
                    fix="Keep equipped gear on death (respawn.equipped.lose_chance), grant usable gear, or soften level loss.")


# ----------------------------------------------------------------------------------------
# quick simulation

def check_sim(ctx: Ctx, n: int = 1500, runs: int = 30, seed: int = 1):
    from ..sim.report import simulate
    eco = ctx.eco
    try:
        res = simulate(eco, n, runs=runs, seed=seed, every=max(1, n // 30))
    except FormulaError as e:
        ctx.add("bad-formula", "respawn", f"a formula failed during the quick simulation: {e}",
                fix="Make the formula valid for every reachable state (level, xp, deaths, amount).")
        return
    s = res.summary()
    tl = s["timeline"]
    ctx.metrics["quick_sim"] = {
        "encounters": n, "runs": runs, "seed": seed,
        "final_level_p50": s["final"]["level_p50"],
        "final_money_p50": s["final"]["money_p50"],
        "deaths_per_100_encounters": round(s["lives"]["deaths_per_100_encounters"], 3),
        "lifespan_mean": s["lives"]["lifespan_mean"],
        "lifespan_p50": s["lives"]["lifespan_p50"],
        "first_life_p50": s["lives"]["first_life_p50"],
        "time_share_by_location": s["time_share_by_location"],
        "levels_reached": {k: v["encounter_p50"] for k, v in s["levels"].items()},
    }
    # stall: longest stretch with constant median level below max
    best = None
    i = 0
    while i < len(tl):
        j = i
        lvl = tl[i]["level"]["p50"]
        while j + 1 < len(tl) and tl[j + 1]["level"]["p50"] == lvl:
            j += 1
        if lvl < eco.max_level and j > i:
            span = tl[j]["encounter"] - tl[i]["encounter"]
            if best is None or span > best[0]:
                best = (span, tl[i]["encounter"], tl[j]["encounter"], lvl, tl[i]["location_mode"])
        i = j + 1
    if best and best[0] >= 0.4 * n:
        ctx.add("progression-stall", "locations",
                f"in the quick simulation the median player is stuck at level {best[3]:.0f} from encounter "
                f"{best[1]:,} to {best[2]:,} (mostly at {best[4]})",
                detail=f"{runs} runs × {n} encounters with player.policy; run `econ sim` for details",
                fix="Check survivability and gear prices around that level (lifespan and upkeep findings), or the XP curve.")
    money = [r["money"]["p50"] for r in tl]
    start_money = money[0] if money else 0
    if len(money) > 4 and money[-1] <= 0.05 * max(max(money), 1) and max(money) > 0:
        ctx.add("bankrupt", "player.policy",
                f"in the quick simulation the median player's {eco.primary} falls to "
                f"{eco.fmt_amount(eco.primary, money[-1])} after peaking at {eco.fmt_amount(eco.primary, max(money))}",
                detail="money flows per run: " + ", ".join(
                    f"{k} {eco.fmt_amount(eco.primary, v.get(eco.primary, 0))}" for k, v in s["flows_per_run"].items()),
                fix="Reduce upkeep (half-lives, prices) or death penalties, or raise income.")
    _ = start_money


ALL_CHECKS = [check_structure, check_battle, check_market, check_lifespan, check_progression, check_income, check_respawn]
