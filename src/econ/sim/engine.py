"""Monte Carlo simulation of a player fighting encounters at a fixed rate.

Each encounter: pick an enemy from the current location's table, roll the battle with the
player's effective level (level + equipped gear power, + a consumable's power if the policy
uses one), apply wear to equipped gear (usage half-life), then either award XP and loot or
apply the respawn rules. Between encounters a simple bot policy (configured in
`player.policy`) equips, sells, crafts, shops and picks where to fight.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import accumulate

from ..economy import Economy, Item, Policy


@dataclass
class Player:
    level: int
    xp: int
    money: dict[str, int]
    inv: dict[str, int]
    equipped: dict[str, str]
    location: str
    deaths: int = 0

    def owns(self, iid: str) -> bool:
        return self.inv.get(iid, 0) > 0 or iid in self.equipped.values()


@dataclass
class Snapshot:
    encounter: int
    level: int
    xp: int
    money: dict[str, int]
    worth: float           # primary currency + items at sell value (primary)
    eff_level: float
    location: str
    deaths: int
    items: int


@dataclass
class RunStats:
    snapshots: list[Snapshot] = field(default_factory=list)
    lifespans: list[int] = field(default_factory=list)       # completed lives
    final_life: int = 0                                       # censored life at the end
    flows: dict[str, dict[str, int]] = field(default_factory=dict)  # category -> currency -> amount
    items: dict[str, dict[str, int]] = field(default_factory=dict)  # category -> item -> count
    kills: dict[str, int] = field(default_factory=dict)
    deaths_at: dict[str, int] = field(default_factory=dict)
    deaths_by: dict[str, int] = field(default_factory=dict)
    time_at: dict[str, int] = field(default_factory=dict)
    level_reached: dict[int, int] = field(default_factory=dict)  # level -> encounter index
    acquired: dict[str, int] = field(default_factory=dict)       # item -> first encounter owned
    log: list[str] = field(default_factory=list)
    final: Player | None = None

    def flow(self, cat: str, cid: str, amt: int):
        if amt:
            d = self.flows.setdefault(cat, {})
            d[cid] = d.get(cid, 0) + amt

    def item(self, cat: str, iid: str, n: int = 1):
        if n:
            d = self.items.setdefault(cat, {})
            d[iid] = d.get(iid, 0) + n

    def got(self, iid: str, enc: int):
        if iid not in self.acquired:
            self.acquired[iid] = enc


TraceFn = "callable"


class Simulator:
    def __init__(self, eco: Economy, policy: Policy | None = None, seed: int = 0,
                 snapshot_every: int = 100, log: bool = False, trace=None, run_id: int = 0):
        self.eco = eco
        self.pol = policy or eco.policy
        self.rng = random.Random(seed)
        self.every = max(1, snapshot_every)
        self.logging = log
        self.trace = trace
        self.run_id = run_id
        self._loc_cache: dict = {}
        self._cum: dict[str, tuple[list, list[float]]] = {}
        for loc in eco.locations.values():
            if loc.variants:
                self._cum[loc.id] = (loc.variants, list(accumulate(v.prob for v in loc.variants)))
        self._slots = eco.gear_slots()
        self._consumables = sorted((i for i in eco.items.values() if i.consumable and i.power > 0),
                                   key=lambda i: -i.power)
        self._recipe_inputs: dict[str, int] = {}
        self._profitable: dict[str, bool] = {}
        for r in eco.recipes.values():
            for g, q in r.inputs.items():
                if g in eco.items:
                    self._recipe_inputs[g] = max(self._recipe_inputs.get(g, 0), q)
            out_v = float(r.success) * sum(self._value(g, q) for g, q in r.outputs.items())
            in_v = sum(self._value(g, q) for g, q in r.inputs.items()) + r.cost.get(eco.primary, 0)
            self._profitable[r.id] = out_v > in_v

    # ------------------------------------------------------------------------------------
    def new_player(self, level: int | None = None, money: dict | None = None, items: dict | None = None,
                   location: str | None = None) -> Player:
        st = self.eco.start
        p = Player(level=level or st.level, xp=st.xp if level is None else 0,
                   money=dict(st.currencies if money is None else money),
                   inv=dict(st.items if items is None else items), equipped={},
                   location=location or st.location)
        if st.equipped is not None and items is None:
            for iid in st.equipped:
                it = self.eco.items[iid]
                if p.inv.get(iid, 0) > 0 and it.slot and it.slot not in p.equipped:
                    p.inv[iid] -= 1
                    p.equipped[it.slot] = iid
        self._equip_best(p)
        for cid in list(p.money):
            cap = self.eco.currencies[cid].cap
            if cap is not None:
                p.money[cid] = min(p.money[cid], cap)
        return p

    def log(self, stats: RunStats, enc: int, msg: str):
        if self.logging and len(stats.log) < 5000:
            stats.log.append(f"[{enc:>6}] {msg}")

    # ------------------------------------------------------------------------------------
    def power(self, p: Player) -> float:
        return sum(self.eco.items[i].power for i in p.equipped.values())

    def worth(self, p: Player) -> float:
        eco = self.eco
        w = float(p.money.get(eco.primary, 0))
        for iid, n in p.inv.items():
            w += n * eco.sell_value(iid)
        for iid in p.equipped.values():
            w += eco.sell_value(iid)
        return w

    def _add_money(self, p: Player, stats: RunStats, cid: str, amt: int, cat: str):
        if amt <= 0:
            return
        cap = self.eco.currencies[cid].cap
        cur = p.money.get(cid, 0)
        new = cur + amt
        if cap is not None and new > cap:
            stats.flow("overflow", cid, new - cap)
            new = cap
        stats.flow(cat, cid, new - cur)
        p.money[cid] = new

    def _can_pay(self, p: Player, price: dict[str, int], times: int = 1) -> bool:
        res = self.pol.reserve
        return all(p.money.get(c, 0) - res.get(c, 0) >= v * times for c, v in price.items())

    def _pay(self, p: Player, stats: RunStats, price: dict[str, int], cat: str, times: int = 1):
        for c, v in price.items():
            p.money[c] = p.money.get(c, 0) - v * times
            stats.flow(cat, c, -v * times)

    # ------------------------------------------------------------------------------------
    def _equip_best(self, p: Player, stats: RunStats | None = None, enc: int = 0):
        items = self.eco.items
        for slot, cands in self._slots.items():
            cur = p.equipped.get(slot)
            best = cur
            best_pow = items[cur].power if cur else -math.inf
            for it in cands:
                if p.inv.get(it.id, 0) > 0 and it.min_level <= p.level and it.power > best_pow:
                    best, best_pow = it.id, it.power
            if best != cur:
                p.inv[best] -= 1
                if cur:
                    p.inv[cur] = p.inv.get(cur, 0) + 1
                p.equipped[slot] = best
                if stats is not None:
                    self.log(stats, enc, f"equip {best} in {slot}" + (f" (was {cur})" if cur else ""))

    def _sell(self, p: Player, stats: RunStats, iid: str, n: int, enc: int):
        it = self.eco.items[iid]
        if n <= 0 or not it.sell or not any(v > 0 for v in it.sell.values()):
            return
        p.inv[iid] -= n
        for cid, v in it.sell.items():
            self._add_money(p, stats, cid, v * n, "sales")
        stats.item("sold", iid, n)

    def _sell_loot(self, p: Player, stats: RunStats, enc: int):
        pol, items = self.pol, self.eco.items
        for iid, n in list(p.inv.items()):
            if n <= 0:
                continue
            it = items[iid]
            if not it.sell:
                continue
            if it.is_gear:
                if not pol.sell_outdated_gear:
                    continue
                eq = p.equipped.get(it.slot)
                # keep gear that is better than equipped (e.g. not yet usable) — sell the rest
                if eq is None or it.power > items[eq].power:
                    continue
                self._sell(p, stats, iid, n, enc)
            elif it.consumable and it.power > 0:
                keep = max(pol.keep_consumables, pol.buy_consumables)
                if n > keep:
                    self._sell(p, stats, iid, n - keep, enc)
            elif pol.sell_loot:
                keep = 0
                if pol.craft != "none" and iid in self._recipe_inputs:
                    keep = 2 * self._recipe_inputs[iid]
                if n > keep:
                    self._sell(p, stats, iid, n - keep, enc)

    def _value(self, gid: str, qty: float) -> float:
        """Liquid value in primary currency."""
        if gid == self.eco.primary:
            return qty
        if gid in self.eco.currencies:
            return 0.0
        return qty * self.eco.sell_value(gid)

    def _max_crafts(self, p: Player, r) -> int:
        """How many times recipe `r` can be crafted right now from inventory and money."""
        eco = self.eco
        n = 10**9
        per_cur: dict[str, int] = dict(r.cost)
        for g, q in r.inputs.items():
            if g in eco.items:
                n = min(n, p.inv.get(g, 0) // q)
            else:
                per_cur[g] = per_cur.get(g, 0) + q
        for c, q in per_cur.items():
            if q > 0:
                n = min(n, max(0, p.money.get(c, 0) - self.pol.reserve.get(c, 0)) // q)
        return n if n < 10**9 else 1

    def _craft(self, p: Player, stats: RunStats, enc: int):
        pol, eco = self.pol, self.eco
        if pol.craft == "none":
            return
        for r in eco.recipes.values():
            if r.min_level > p.level:
                continue
            want = pol.craft in ("profitable", "all") and self._profitable[r.id]
            gear_target = None
            if pol.craft in ("gear", "all"):
                for g in r.outputs:
                    it = eco.items.get(g)
                    if it and it.slot and it.min_level <= p.level:
                        cur = p.equipped.get(it.slot)
                        if cur is None or it.power > eco.items[cur].power:
                            gear_target = g
                            want = True
            if not want:
                continue
            for _round in range(20):  # outputs may feed inputs (refining chains, duplication loops)
                n = self._max_crafts(p, r)
                if n <= 0 and gear_target and self._buy_inputs(p, stats, r, enc):
                    n = self._max_crafts(p, r)
                if gear_target:
                    n = min(n, 1)
                if n <= 0:
                    break
                self._pay(p, stats, r.cost, "craft fees", n)
                for g, q in r.inputs.items():
                    if g in eco.items:
                        p.inv[g] -= q * n
                        stats.item("crafting input", g, q * n)
                    else:
                        p.money[g] -= q * n
                        stats.flow("craft inputs", g, -q * n)
                ok = n if r.success >= 1 else self.rng.binomialvariate(n, float(r.success))
                for g, q in r.outputs.items():
                    if g in eco.items:
                        p.inv[g] = p.inv.get(g, 0) + q * ok
                        stats.item("crafted", g, q * ok)
                        if ok:
                            stats.got(g, enc)
                    else:
                        self._add_money(p, stats, g, q * ok, "craft outputs")
                self.log(stats, enc, f"craft {r.id}" + (f" ×{n}" if n > 1 else "") + (f" ({n - ok} failed)" if ok < n else ""))
                if gear_target:
                    self._equip_best(p, stats, enc)
                    break

    def _buy_inputs(self, p: Player, stats: RunStats, r, enc: int) -> bool:
        eco = self.eco
        need: dict[str, int] = {}
        for g, q in r.inputs.items():
            if g in eco.items:
                short = q - p.inv.get(g, 0)
                if short > 0:
                    if eco.items[g].buy is None:
                        return False
                    need[g] = short
        total: dict[str, int] = dict(r.cost)
        for g, n in need.items():
            for c, v in eco.items[g].buy.items():
                total[c] = total.get(c, 0) + v * n
        if not self._can_pay(p, total):
            return False
        for g, n in need.items():
            self._pay(p, stats, eco.items[g].buy, "purchases", n)
            p.inv[g] = p.inv.get(g, 0) + n
            stats.item("bought", g, n)
            stats.got(g, enc)
        return True

    def _buy_gear(self, p: Player, stats: RunStats, enc: int):
        items = self.eco.items
        for slot, cands in self._slots.items():
            cur = p.equipped.get(slot)
            cur_pow = items[cur].power if cur else -math.inf
            best = None
            for it in cands:
                if it.buy is None or it.min_level > p.level or it.power <= cur_pow:
                    continue
                if not self._can_pay(p, it.buy):
                    continue
                if best is None or it.power > best.power or (it.power == best.power and _price(it) < _price(best)):
                    best = it
            if best is not None:
                self._pay(p, stats, best.buy, "purchases")
                p.inv[best.id] = p.inv.get(best.id, 0) + 1
                stats.item("bought", best.id)
                stats.got(best.id, enc)
                self.log(stats, enc, f"buy {best.id} for {self.eco.fmt(best.buy)}")
                self._equip_best(p, stats, enc)

    def _buy_consumables(self, p: Player, stats: RunStats, enc: int):
        want = self.pol.buy_consumables
        if want <= 0 or not self._consumables:
            return
        have = sum(p.inv.get(c.id, 0) for c in self._consumables)
        buyable = [c for c in self._consumables if c.buy is not None and c.min_level <= p.level]
        if not buyable:
            return
        c = max(buyable, key=lambda i: (i.power / max(1, _price(i)), i.power))
        while have < want and self._can_pay(p, c.buy):
            self._pay(p, stats, c.buy, "purchases")
            p.inv[c.id] = p.inv.get(c.id, 0) + 1
            stats.item("bought", c.id)
            stats.got(c.id, enc)
            have += 1

    def _choose_location(self, p: Player) -> str:
        pol, eco = self.pol, self.eco
        if pol.location != "auto":
            return pol.location
        pw = round(self.power(p), 4)
        key = (p.level, pw)
        hit = self._loc_cache.get(key)
        if hit:
            return hit
        eff = p.level + pw
        best, best_score, safest, safest_q = None, None, None, None
        rows = []
        for loc in eco.locations.values():
            if loc.safe or not loc.variants:
                continue
            q = xp = inc = 0.0
            for v in loc.variants:
                pr = eco.odds(eff, v.level)
                q += v.prob * (1 - pr)
                e = eco.enemies[v.enemy]
                xp += v.prob * pr * eco.xp_gain(e.xp, p.level, v.level)
                inc += v.prob * pr * self._enemy_value(v.enemy)
            rows.append((loc.id, q, xp, inc))
            if safest_q is None or q < safest_q:
                safest, safest_q = loc.id, q
        if not rows:
            return p.location
        feasible = [r for r in rows if r[1] <= pol.max_death_risk]
        if not feasible:
            self._loc_cache[key] = safest
            return safest
        mx_xp = max(r[2] for r in feasible) or 1.0
        mx_inc = max(r[3] for r in feasible) or 1.0
        for lid, q, xp, inc in feasible:
            if pol.objective == "xp":
                score = (xp, inc, -q)
            elif pol.objective == "income":
                score = (inc, xp, -q)
            else:
                score = (xp / mx_xp + inc / mx_inc, -q)
            if best_score is None or score > best_score:
                best, best_score = lid, score
        self._loc_cache[key] = best
        return best

    def _enemy_value(self, eid: str) -> float:
        v = 0.0
        for d in self.eco.enemies[eid].drops:
            v += d.mean_qty * (1.0 if d.id == self.eco.primary else
                               (self.eco.sell_value(d.id) if d.kind == "item" else 0.0))
        return v

    def tick(self, p: Player, stats: RunStats, enc: int):
        """Between-encounter decisions."""
        self._equip_best(p, stats, enc)
        self._craft(p, stats, enc)
        self._sell_loot(p, stats, enc)
        if self.pol.buy_gear:
            self._buy_gear(p, stats, enc)
        self._buy_consumables(p, stats, enc)
        self._sell_loot(p, stats, enc)
        new_loc = self._choose_location(p)
        if new_loc != p.location:
            self.log(stats, enc, f"move {p.location} -> {new_loc} (level {p.level}, power {self.power(p):g})")
            p.location = new_loc

    # ------------------------------------------------------------------------------------
    def _snapshot(self, p: Player, enc: int) -> Snapshot:
        return Snapshot(enc, p.level, p.xp, dict(p.money), self.worth(p), p.level + self.power(p), p.location,
                        p.deaths, sum(p.inv.values()) + len(p.equipped))

    def run(self, n: int, player: Player | None = None) -> RunStats:
        eco, pol, rng = self.eco, self.pol, self.rng
        p = player or self.new_player()
        stats = RunStats()
        stats.level_reached[p.level] = 0
        for iid in list(p.inv) + list(p.equipped.values()):
            stats.got(iid, 0)
        self.tick(p, stats, 0)
        stats.snapshots.append(self._snapshot(p, 0))
        life_start = 0
        need_tick = False
        for enc in range(1, n + 1):
            if need_tick or enc % pol.shop_every == 0:
                self.tick(p, stats, enc)
                need_tick = False
            loc = eco.locations.get(p.location)
            if loc is None or loc.id not in self._cum:
                p.location = self._choose_location(p)
                loc = eco.locations.get(p.location)
                if loc is None or loc.id not in self._cum:
                    continue
            stats.time_at[loc.id] = stats.time_at.get(loc.id, 0) + 1
            variants, cum = self._cum[loc.id]
            v = variants[min(bisect_right(cum, rng.random() * cum[-1]), len(variants) - 1)]
            power = self.power(p)
            eff = p.level + power
            pw = eco.odds(eff, v.level)
            used = None
            if pw < pol.use_consumables_below:
                for c in self._consumables:
                    if p.inv.get(c.id, 0) > 0 and c.min_level <= p.level:
                        used = c
                        eff += c.power
                        pw = eco.odds(eff, v.level)
                        break
            win = rng.random() < pw
            # wear and tear
            broke = []
            for slot, iid in list(p.equipped.items()):
                b = eco.items[iid].break_chance
                if b and rng.random() < b:
                    del p.equipped[slot]
                    stats.item("broken", iid)
                    broke.append(iid)
                    need_tick = True
            if used is not None:
                stats.item("used", used.id)
                if rng.random() < used.break_chance:
                    p.inv[used.id] -= 1
                    stats.item("consumed", used.id)
            if broke:
                self.log(stats, enc, f"{', '.join(broke)} broke")
            drops_s = ""
            if win:
                e = eco.enemies[v.enemy]
                stats.kills[v.enemy] = stats.kills.get(v.enemy, 0) + 1
                gained = eco.xp_gain(e.xp, p.level, v.level)
                if p.level < eco.max_level:
                    p.xp += gained
                    while p.level < eco.max_level and p.xp >= eco.xp_to_next(p.level):
                        p.xp -= eco.xp_to_next(p.level)
                        p.level += 1
                        stats.level_reached.setdefault(p.level, enc)
                        self.log(stats, enc, f"level up -> {p.level}")
                        need_tick = True
                    if p.level >= eco.max_level:
                        p.xp = 0
                got = []
                for d in e.drops:
                    if d.chance >= 1 or rng.random() < d.chance:
                        q = d.qmin if d.qmin == d.qmax else rng.randint(d.qmin, d.qmax)
                        if q <= 0:
                            continue
                        if d.kind == "currency":
                            self._add_money(p, stats, d.id, q, "drops")
                        else:
                            p.inv[d.id] = p.inv.get(d.id, 0) + q
                            stats.item("dropped", d.id, q)
                            stats.got(d.id, enc)
                        got.append(f"{d.id}×{q}")
                drops_s = " ".join(got)
            else:
                stats.lifespans.append(enc - life_start)
                life_start = enc
                self._die(p, stats, enc, loc.id, v.enemy, v.level)
                need_tick = True
            if self.trace is not None:
                self.trace({
                    "run": self.run_id, "encounter": enc, "location": loc.id, "enemy": v.enemy,
                    "enemy_level": v.level, "eff_level": round(eff, 3), "p_win": round(pw, 5),
                    "outcome": "win" if win else "death", "consumable": used.id if used else "",
                    "broke": " ".join(broke), "drops": drops_s, "level": p.level, "xp": p.xp,
                    "money": p.money.get(eco.primary, 0), "worth": round(self.worth(p), 2), "deaths": p.deaths,
                })
            if enc % self.every == 0 or enc == n:
                stats.snapshots.append(self._snapshot(p, enc))
        stats.final_life = n - life_start
        stats.final = p
        return stats

    def _die(self, p: Player, stats: RunStats, enc: int, where: str, enemy: str, enemy_level: int):
        eco, rng, r = self.eco, self.rng, self.eco.respawn
        p.deaths += 1
        stats.deaths_at[where] = stats.deaths_at.get(where, 0) + 1
        stats.deaths_by[enemy] = stats.deaths_by.get(enemy, 0) + 1
        old_level, old_xp = p.level, p.xp
        env = {"level": float(old_level), "xp": float(old_xp), "deaths": float(p.deaths)}
        new_level = int(math.floor(r.level.eval(env) + 1e-9))
        new_level = min(max(1, new_level), eco.max_level)
        env["xpToNext"] = float(eco.xp_to_next(old_level) or 1)
        new_xp = int(math.floor(r.xp.eval(env) + 1e-9))
        top = eco.xp_to_next(new_level)
        new_xp = 0 if top == 0 else min(max(0, new_xp), top - 1)
        p.level, p.xp = new_level, new_xp
        if new_level < old_level:
            for slot, iid in list(p.equipped.items()):
                if eco.items[iid].min_level > new_level:   # can no longer use it: back to the bag
                    del p.equipped[slot]
                    p.inv[iid] = p.inv.get(iid, 0) + 1
        lost_parts = []
        for cid, f in r.currencies.items():
            cur = p.money.get(cid, 0)
            cap = eco.currencies[cid].cap
            nv = f.eval({"amount": float(cur), "cap": float(cap if cap is not None else 10**12),
                         "level": float(old_level), "deaths": float(p.deaths)})
            nv = max(0, int(math.floor(nv + 1e-9)))
            if cap is not None:
                nv = min(nv, cap)
            if nv < cur:
                stats.flow("death penalty", cid, nv - cur)
                lost_parts.append(eco.fmt_amount(cid, cur - nv))
            elif nv > cur:
                stats.flow("respawn grants", cid, nv - cur)
            p.money[cid] = nv
        if r.inv_lose > 0:
            for iid, n in list(p.inv.items()):
                if n > 0 and iid not in r.inv_protect:
                    lost = rng.binomialvariate(n, r.inv_lose)
                    if lost:
                        p.inv[iid] -= lost
                        stats.item("lost on death", iid, lost)
                        lost_parts.append(f"{iid}×{lost}")
        if r.eq_lose > 0:
            for slot, iid in list(p.equipped.items()):
                if iid not in r.eq_protect and rng.random() < r.eq_lose:
                    del p.equipped[slot]
                    stats.item("lost on death", iid)
                    lost_parts.append(iid)
        for g in r.grants:
            if g.kind == "item":
                if g.if_missing and p.owns(g.id):
                    continue
                p.inv[g.id] = p.inv.get(g.id, 0) + g.qty
                stats.item("granted", g.id, g.qty)
                stats.got(g.id, enc)
            else:
                cur = p.money.get(g.id, 0)
                if g.if_missing and cur >= g.qty:
                    continue
                add = g.qty - cur if g.if_missing else g.qty
                self._add_money(p, stats, g.id, add, "respawn grants")
        if r.location:
            p.location = r.location
        lvl_s = f", level {old_level}->{new_level}" if new_level != old_level else ""
        self.log(stats, enc, f"DIED to {enemy} (L{enemy_level}) at {where}{lvl_s}"
                 + (f", lost {', '.join(lost_parts)}" if lost_parts else ""))


def _price(it: Item) -> int:
    return sum(it.buy.values()) if it.buy else 0
