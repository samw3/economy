"""Respawn analysis: is dying ever profitable, can the penalty be dodged, do deaths snowball?"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..economy import Economy
from ..formula import FormulaError


def currency_after_death(eco: Economy, cid: str, amount: int, level: int = 1, deaths: int = 1) -> int:
    f = eco.respawn.currencies.get(cid)
    if f is None:
        return amount
    cap = eco.currencies[cid].cap
    v = f.eval({"amount": float(amount), "cap": float(cap if cap is not None else 10**12),
                "level": float(level), "deaths": float(deaths)})
    v = int(math.floor(v + 1e-9))
    v = max(0, v)
    if cap is not None:
        v = min(v, cap)
    return v


def death_loss(eco: Economy, cid: str, amount: int) -> int:
    """Currency lost on death when holding `amount` (negative = death *pays*)."""
    return amount - currency_after_death(eco, cid, amount)


def grant_value(eco: Economy) -> dict[str, int]:
    """Liquid value of respawn grants (granted items valued at their sell price)."""
    out: dict[str, int] = {}
    for g in eco.respawn.grants:
        if g.kind == "currency":
            out[g.id] = out.get(g.id, 0) + g.qty
        else:
            for cid, v in (eco.items[g.id].sell or {}).items():
                out[cid] = out.get(cid, 0) + v * g.qty
    return out


def _sample_amounts(cap: int | None) -> list[int]:
    top = cap if cap is not None else 10**7
    pts = set(range(0, min(top, 2000) + 1))
    x = 2000.0
    while x < top:
        pts.add(int(x))
        x *= 1.05
    pts.add(top)
    return sorted(pts)


@dataclass
class DeathFarming:
    currency: str
    grant: dict[str, int]
    profitable_below: int            # dying is profitable while holding less than this
    profit_when_broke: int           # profit of one death when holding 0
    xp_cost: str = ""
    deaths_per_encounter: float = 0.0
    farm_rate: float = 0.0           # profit per encounter when farming deaths
    normal_rate: float = 0.0         # best normal income per encounter for a level-1 player
    grant_items: list[str] = field(default_factory=list)


def respawn_currency_after(eco: Economy, cid: str, amount: int) -> int:
    """Holding of `cid` right after a respawn, starting from `amount`: penalty formula, then
    currency grants (if_missing = top up), capped by the wallet."""
    after = currency_after_death(eco, cid, amount)
    for g in eco.respawn.grants:
        if g.kind == "currency" and g.id == cid:
            after = max(after, g.qty) if g.if_missing else after + g.qty
    cap = eco.currencies[cid].cap
    if cap is not None:
        after = min(after, cap)
    return after


def max_respawn_currency_gain(eco: Economy, cid: str) -> int:
    """Largest increase of `cid` a single respawn can cause (0 if respawning never adds money).
    Respawn formulas are evaluated on every amount up to 2,000 and a geometric grid above."""
    involved = cid in eco.respawn.currencies or any(
        g.kind == "currency" and g.id == cid for g in eco.respawn.grants)
    if not involved:
        return 0
    try:
        return max(0, max(respawn_currency_after(eco, cid, a) - a for a in _sample_amounts(eco.currencies[cid].cap)))
    except FormulaError:
        return 0


def death_profit(eco: Economy, cid: str, amount: int) -> int:
    """Change in liquid value (currency `cid`) from one death while holding `amount` and no
    granted items: penalty formula, currency grants, and granted items at their sell price."""
    items = 0
    for g in eco.respawn.grants:
        if g.kind == "item":
            items += (eco.items[g.id].sell or {}).get(cid, 0) * g.qty
    return respawn_currency_after(eco, cid, amount) - amount + items


def death_farming(eco: Economy, best_income_lvl1: float, max_death_prob_lvl1: float) -> DeathFarming | None:
    gv = grant_value(eco)
    cid = eco.primary
    cap = eco.currencies[cid].cap
    try:
        amounts = _sample_amounts(cap)
        profits = [(a, death_profit(eco, cid, a)) for a in amounts]
    except FormulaError:
        return None
    good = [a for a, p in profits if p > 0]
    if not good:
        return None
    below = max(good) + 1
    broke = death_profit(eco, cid, 0)
    xp_note = ""
    src = eco.respawn.xp.source.strip()
    if src not in ("xp",):
        xp_note = f"each death also applies respawn.xp = {src!r}"
    if eco.respawn.level.source.strip() != "level":
        xp_note += ("; " if xp_note else "") + f"respawn.level = {eco.respawn.level.source!r}"
    rate = max(0.0, broke) * max_death_prob_lvl1
    return DeathFarming(cid, gv, below, broke, xp_note, max_death_prob_lvl1, rate, best_income_lvl1,
                        [g.id for g in eco.respawn.grants if g.kind == "item"])


@dataclass
class PenaltyEvasion:
    currency: str
    penalty_rate: float          # fraction of holdings lost per death at the reference amount
    reference_amount: int
    item: str
    spread: float                # round-trip loss buying then selling the item
    item_loss: float             # chance the item itself is lost on death


def penalty_evasion(eco: Economy) -> PenaltyEvasion | None:
    cid = eco.primary
    if cid not in eco.respawn.currencies:
        return None
    cap = eco.currencies[cid].cap
    ref = min(cap, 100_000) if cap else 100_000
    ref = max(ref // 2, 1)
    try:
        loss = death_loss(eco, cid, ref)
    except FormulaError:
        return None
    rate = loss / ref
    if rate <= 0:
        return None
    best = None
    for it in eco.items.values():
        if it.buy is None or it.sell is None:
            continue
        b, s = it.buy.get(cid), it.sell.get(cid)
        if not b or s is None or set(it.buy) != {cid}:
            continue
        spread = 1 - s / b
        if spread < 0:
            continue  # buy-below-sell is reported as an exploit on its own
        lost = 0.0 if it.id in eco.respawn.inv_protect else eco.respawn.inv_lose
        cost = spread + lost
        if cost < rate and (best is None or cost < best[0]):
            best = (cost, it.id, spread, lost)
    if best is None:
        return None
    return PenaltyEvasion(cid, rate, ref, best[1], best[2], best[3])
