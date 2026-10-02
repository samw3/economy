"""Combat analysis: encounter tables, death probability, lifespans, XP and income rates.

Lifespan = expected number of encounters in one life (from spawn until death, counting the
fatal encounter). At a fixed effective level with per-encounter death probability q this is the
mean of a geometric distribution, 1/q. `spawn_study` additionally accounts for level-ups during
the life with an exact dynamic program over (level, xp) states.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lfilter

from ..economy import Economy, Item, Location

IMMORTAL_EPS = 1e-12  # per-encounter death probabilities below this are treated as exactly 0


# ----------------------------------------------------------------------------------------
# sources and loadouts

def placed_enemies(eco: Economy) -> set[str]:
    return {v.enemy for l in eco.locations.values() if not l.safe for v in l.variants}


def item_sources(eco: Economy) -> dict[str, list[str]]:
    """How each item can be obtained (empty list = unobtainable)."""
    src: dict[str, list[str]] = {i: [] for i in eco.items}
    placed = placed_enemies(eco)
    for iid, it in eco.items.items():
        if it.buy is not None and affordable(eco, it.buy):
            src[iid].append("buy")
    for eid in sorted(placed):
        for d in eco.enemies[eid].drops:
            if d.kind == "item" and d.chance > 0 and d.qmax > 0:
                src[d.id].append(f"drop:{eid}")
    for g in eco.respawn.grants:
        if g.kind == "item":
            src[g.id].append("respawn grant")
    for iid, q in eco.start.items.items():
        if q > 0:
            src[iid].append("start")
    currencies = currency_sources(eco)
    changed = True
    while changed:
        changed = False
        for r in eco.recipes.values():
            ok = all((src.get(g) if g in eco.items else currencies.get(g)) for g in r.inputs)
            ok = ok and all(currencies.get(c) for c in r.cost)
            if not ok:
                continue
            for o in r.outputs:
                if o in eco.items and f"craft:{r.id}" not in src[o]:
                    src[o].append(f"craft:{r.id}")
                    changed = True
    return src


def affordable(eco: Economy, price: dict[str, int]) -> bool:
    """A price fits in the wallets (prices above a wallet cap can never be paid)."""
    for cid, v in price.items():
        cap = eco.currencies[cid].cap
        if cap is not None and v > cap:
            return False
    return True


def currency_sources(eco: Economy) -> dict[str, list[str]]:
    src: dict[str, list[str]] = {c: [] for c in eco.currencies}
    placed = placed_enemies(eco)
    for cid, amt in eco.start.currencies.items():
        if amt > 0:
            src[cid].append("start")
    for eid in sorted(placed):
        for d in eco.enemies[eid].drops:
            if d.kind == "currency" and d.chance > 0 and d.qmax > 0:
                src[d.id].append(f"drop:{eid}")
    for it in eco.items.values():
        for cid, v in (it.sell or {}).items():
            if v > 0:
                src[cid].append(f"sell:{it.id}")
    for r in eco.recipes.values():
        for o in r.outputs:
            if o in eco.currencies:
                src[o].append(f"craft:{r.id}")
    for g in eco.respawn.grants:
        if g.kind == "currency":
            src[g.id].append("respawn grant")
    return src


def obtainable_items(eco: Economy) -> set[str]:
    return {i for i, s in item_sources(eco).items() if s}


@dataclass
class Loadout:
    name: str
    items: dict[str, Item]
    power: float

    def describe(self) -> str:
        if not self.items:
            return "no gear"
        return ", ".join(f"{i.id} (+{i.power:g})" for i in self.items.values())


def loadout(eco: Economy, level: int, kind: str, pool: set[str] | None = None) -> Loadout:
    if kind == "naked":
        return Loadout("naked", {}, 0.0)
    items = eco.best_loadout(level, pool)
    return Loadout("geared", items, eco.loadout_power(items))


# ----------------------------------------------------------------------------------------
# per-encounter quantities

def drop_value(eco: Economy, enemy_id: str) -> dict[str, float]:
    """Expected liquid value (currency drops + item drops at sell price) per kill, by currency."""
    out: dict[str, float] = {}
    for d in eco.enemies[enemy_id].drops:
        if d.kind == "currency":
            out[d.id] = out.get(d.id, 0.0) + d.mean_qty
        else:
            for cid, v in (eco.items[d.id].sell or {}).items():
                out[cid] = out.get(cid, 0.0) + d.mean_qty * v
    return out


def max_drop(eco: Economy, enemy_id: str) -> dict[str, int]:
    """Everything an enemy can drop at once (each roll succeeding at max quantity)."""
    out: dict[str, int] = {}
    for d in eco.enemies[enemy_id].drops:
        if d.chance > 0 and d.qmax > 0:
            out[d.id] = out.get(d.id, 0) + d.qmax
    return out


@dataclass
class EncounterStats:
    location: str
    level: int
    power: float
    death_prob: float
    xp: float                         # expected XP per encounter
    income: dict[str, float]          # expected liquid value per encounter, by currency
    hardest_win: float                # lowest win probability among variants
    easiest_win: float

    @property
    def lifespan(self) -> float:
        return math.inf if self.death_prob <= 0 else 1.0 / self.death_prob


def encounter_stats(eco: Economy, loc: Location, level: int, power: float) -> EncounterStats:
    eff = level + power
    q = 0.0
    xp = 0.0
    income: dict[str, float] = {}
    lo, hi = 1.0, 0.0
    for v in loc.variants:
        p = eco.odds(eff, v.level)
        lo, hi = min(lo, p), max(hi, p)
        q += v.prob * (1 - p)
        e = eco.enemies[v.enemy]
        xp += v.prob * p * eco.xp_gain(e.xp, level, v.level)
        for cid, val in drop_value(eco, v.enemy).items():
            income[cid] = income.get(cid, 0.0) + v.prob * p * val
    q = 0.0 if q < IMMORTAL_EPS else q
    return EncounterStats(loc.id, level, power, q, xp, income, lo, hi)


def replacement_cost(eco: Economy, item: Item) -> float:
    """Primary-currency cost to replace an item: buy price, else crafting cost, else sell value."""
    if item.buy is not None:
        return float(item.buy.get(eco.primary, 0))
    for r in eco.recipes.values():
        if item.id in r.outputs:
            cost = float(r.cost.get(eco.primary, 0))
            for g, q in r.inputs.items():
                if g == eco.primary:
                    cost += q
                elif g in eco.items:
                    it = eco.items[g]
                    unit = it.buy.get(eco.primary, 0) if it.buy is not None else (it.sell or {}).get(eco.primary, 0)
                    cost += q * unit
            return cost / max(1, r.outputs[item.id]) / float(r.success)
    return float((item.sell or {}).get(eco.primary, 0))


def upkeep_per_encounter(eco: Economy, lo: Loadout) -> float:
    return sum(i.break_chance * replacement_cost(eco, i) for i in lo.items.values())


# ----------------------------------------------------------------------------------------
# spawn study: exact expected lifespan with level-ups

@dataclass
class SpawnStudy:
    location: str
    start_level: int
    loadout: str
    lifespan: float                   # E[encounters until death]
    death_level: float                # E[level at death]
    approx: bool = False
    note: str = ""


def spawn_study(eco: Economy, loc: Location, start_level: int, kind: str,
                pool: set[str] | None = None, max_states: int = 400_000) -> SpawnStudy:
    """Expected encounters until death for a player who spawns at `start_level` (0 XP) and fights
    only at `loc`, levelling up along the way. Gear: `kind` loadout re-evaluated at each level."""
    L0, Lmax = start_level, eco.max_level
    total_states = sum(eco.xp_to_next(l) for l in range(L0, Lmax))
    bucket = max(1, math.ceil(total_states / max_states))

    def level_params(l: int):
        lo = loadout(eco, l, kind, pool)
        eff = l + lo.power
        pis: dict[int, float] = {}
        q = 1.0
        for v in loc.variants:
            p = eco.odds(eff, v.level)
            pi = v.prob * p
            q -= pi
            if pi <= 0:
                continue
            g = eco.xp_gain(eco.enemies[v.enemy].xp, l, v.level)
            g = 0 if g <= 0 else max(1, int(round(g / bucket)))
            pis[g] = pis.get(g, 0.0) + pi
        return (0.0 if q < IMMORTAL_EPS else q), pis

    # the highest level the player can actually reach: progress stops where no win gives XP
    top = Lmax
    for l in range(L0, Lmax):
        _, pis = level_params(l)
        if sum(pi for g, pi in pis.items() if g > 0) <= IMMORTAL_EPS:
            top = l
            break
    # value arrays for each level (index = xp // bucket); at `top` the player is stuck: geometric
    T: dict[int, np.ndarray | float] = {}
    D: dict[int, np.ndarray | float] = {}
    q_top, _ = level_params(top)
    if q_top <= 0:
        T[top], D[top] = math.inf, 0.0     # never dies once there
    else:
        T[top], D[top] = 1.0 / q_top, float(top)

    def value_next(level: int, z: np.ndarray, store: dict) -> np.ndarray:
        out = np.empty(len(z))
        lvl = np.full(len(z), level)
        zz = z.copy()
        # cascade through levels while xp overflows (never beyond `top`)
        while True:
            done = True
            for i in range(len(zz)):
                l = lvl[i]
                if l >= top:
                    continue
                X = max(1, eco.xp_to_next(l) // bucket)
                if zz[i] >= X:
                    zz[i] -= X
                    lvl[i] = l + 1
                    done = False
            if done:
                break
        for i in range(len(zz)):
            v = store[min(lvl[i], top)]
            out[i] = v if isinstance(v, float) else v[zz[i]]
        return out

    for l in range(top - 1, L0 - 1, -1):
        q, pis = level_params(l)
        X = max(1, eco.xp_to_next(l) // bucket)
        p0 = pis.get(0, 0.0)
        if 1 - p0 <= IMMORTAL_EPS:
            return SpawnStudy(loc.id, L0, kind, math.inf, float(l), bucket > 1,
                              f"the player can never die or level up at level {l}")
        order = min(X, max(pis) if pis else 0)
        a = np.zeros(order + 1)
        a[0] = 1.0 - p0
        for g, pi in pis.items():
            if 0 < g <= order:
                a[g] -= pi
        bT = np.ones(X)
        bD = np.full(X, q * l)
        ys = np.arange(X)
        for g, pi in pis.items():
            if g <= 0:
                continue
            m = ys < g
            if not m.any():
                continue
            yb = ys[m]
            z = (X - 1 - yb) + g - X
            bT[m] += pi * value_next(l + 1, z, T)
            bD[m] += pi * value_next(l + 1, z, D)
        UT = lfilter([1.0], a, bT)
        UD = lfilter([1.0], a, bD)
        T[l] = UT[::-1].copy()
        D[l] = UD[::-1].copy()

    t0 = T[L0]
    d0 = D[L0]
    life = float(t0 if isinstance(t0, float) else t0[0])
    dlev = float(d0 if isinstance(d0, float) else d0[0])
    notes = []
    if bucket > 1:
        notes.append(f"XP bucketed by {bucket}")
    if math.isinf(life):
        notes.append(f"the player can become immortal (at level {top})")
    elif top < Lmax:
        notes.append(f"progress stops at level {top} (no XP there)")
    return SpawnStudy(loc.id, L0, kind, life, dlev, bucket > 1, "; ".join(notes))


# ----------------------------------------------------------------------------------------

def intended_locations(eco: Economy, level: int) -> list[Location]:
    return [l for l in eco.location_order() if l.level_range and l.level_range[0] <= level <= l.level_range[1]]


def level_coverage_gaps(eco: Economy) -> list[tuple[int, int]]:
    covered = set()
    for l in eco.location_order():
        if l.level_range:
            covered.update(range(l.level_range[0], l.level_range[1] + 1))
    gaps: list[tuple[int, int]] = []
    start = None
    for lvl in range(1, eco.max_level + 1):
        if lvl not in covered:
            start = lvl if start is None else start
        elif start is not None:
            gaps.append((start, lvl - 1))
            start = None
    if start is not None:
        gaps.append((start, eco.max_level))
    return gaps


@dataclass
class LocationProfile:
    location: str
    level_range: tuple[int, int] | None
    rows: list[dict] = field(default_factory=list)  # per level: naked/geared stats
