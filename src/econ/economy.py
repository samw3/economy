"""Compiled economy: references resolved, money in base units, formulas compiled.

`compile_economy(load_result)` turns a validated `EconomyDef` into an `Economy` and reports
semantic problems (unknown references, bad money, bad formulas) as findings.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .formula import Formula, FormulaError
from .lint.findings import Finding, fmt_path
from .loader import LoadResult, SourceMap
from .model import EconomyDef
from .money import Bundle, Currency, Denomination, MoneyError, MoneyParser

BATTLE_VARS = frozenset({"playerLevel", "enemyLevel", "delta"})
XP_TO_NEXT_VARS = frozenset({"level"})
XP_GAIN_VARS = frozenset({"xp", "playerLevel", "enemyLevel", "delta"})
RESPAWN_LEVEL_VARS = frozenset({"level", "xp", "deaths"})
RESPAWN_XP_VARS = frozenset({"level", "xp", "deaths", "xpToNext"})
RESPAWN_CURRENCY_VARS = frozenset({"amount", "cap", "level", "deaths"})


# ----------------------------------------------------------------------------------------
# compiled entities
# ----------------------------------------------------------------------------------------

@dataclass
class Item:
    id: str
    buy: Bundle | None = None
    sell: Bundle | None = None
    slot: str | None = None
    power: float = 0.0
    half_life: float | None = None
    min_level: int = 1
    consumable: bool = False
    tags: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def is_gear(self) -> bool:
        return self.slot is not None

    @property
    def break_chance(self) -> float:
        """Probability the item breaks/is used up on each use."""
        if self.half_life is None:
            return 1.0 if self.consumable else 0.0
        if self.half_life == 0:
            return 1.0
        return 1.0 - 0.5 ** (1.0 / self.half_life)

    @property
    def expected_uses(self) -> float:
        b = self.break_chance
        return math.inf if b == 0 else 1.0 / b


@dataclass
class Recipe:
    id: str
    inputs: dict[str, int]
    outputs: dict[str, int]
    cost: Bundle
    success: Fraction = Fraction(1)
    min_level: int = 1
    description: str = ""


@dataclass
class Drop:
    kind: str  # "item" | "currency"
    id: str
    qmin: int
    qmax: int
    chance: float

    @property
    def mean_qty(self) -> float:
        return self.chance * (self.qmin + self.qmax) / 2.0


@dataclass
class Enemy:
    id: str
    level: int
    xp: float
    drops: list[Drop] = field(default_factory=list)
    description: str = ""


@dataclass
class Encounter:
    enemy: str
    weight: float
    lmin: int
    lmax: int


@dataclass
class Variant:
    """One concrete thing that can be met at a location: an enemy at a specific level."""
    enemy: str
    level: int
    prob: float


@dataclass
class Location:
    id: str
    level_range: tuple[int, int] | None
    encounters: list[Encounter]
    rate: float
    safe: bool = False
    description: str = ""
    variants: list[Variant] = field(default_factory=list)

    def build_variants(self):
        total = sum(e.weight for e in self.encounters)
        out: list[Variant] = []
        for e in self.encounters:
            n = e.lmax - e.lmin + 1
            for lvl in range(e.lmin, e.lmax + 1):
                out.append(Variant(e.enemy, lvl, e.weight / total / n))
        self.variants = out


@dataclass
class Grant:
    kind: str  # item | currency
    id: str
    qty: int
    if_missing: bool = False


@dataclass
class Respawn:
    location: str | None
    level: Formula
    xp: Formula
    currencies: dict[str, Formula]
    inv_lose: float
    inv_protect: set[str]
    eq_lose: float
    eq_protect: set[str]
    grants: list[Grant]

    def is_identity_level(self) -> bool:
        return self.level.source.strip() == "level"


@dataclass
class Start:
    level: int
    xp: int
    location: str
    currencies: Bundle
    items: dict[str, int]
    equipped: list[str] | None


@dataclass
class Policy:
    location: str = "auto"
    max_death_risk: float = 0.03
    objective: str = "xp"
    sell_loot: bool = True
    sell_outdated_gear: bool = True
    buy_gear: bool = True
    buy_consumables: int = 0
    use_consumables_below: float = 0.5
    keep_consumables: int = 5
    craft: str = "profitable"
    reserve: Bundle = field(default_factory=dict)
    shop_every: int = 10


@dataclass
class Targets:
    lifespan: tuple[float | None, float | None] | None = (15, 400)
    even_fight_win: tuple[float | None, float | None] | None = (0.5, 0.9)
    encounters_per_level: tuple[float | None, float | None] | None = (10, 400)
    hours_to_max_level: tuple[float | None, float | None] | None = None
    max_upkeep_ratio: float | None = 0.5
    wallet_fill_encounters: float | None = 50


@dataclass
class IgnoreRule:
    code: str
    path: str | None = None


# ----------------------------------------------------------------------------------------
# the economy
# ----------------------------------------------------------------------------------------

@dataclass
class Economy:
    name: str
    encounter_rate: float
    currencies: dict[str, Currency]
    primary: str
    items: dict[str, Item]
    recipes: dict[str, Recipe]
    enemies: dict[str, Enemy]
    locations: dict[str, Location]
    battle: Formula
    max_level: int
    xp_table: list[int]  # xp_table[level] = XP to go from level to level+1 (index 0 unused)
    xp_gain_formula: Formula
    respawn: Respawn
    start: Start
    policy: Policy
    targets: Targets
    ignore: list[IgnoreRule]
    source: SourceMap
    files: list[str]
    money: MoneyParser
    _odds_cache: dict = field(default_factory=dict, repr=False)
    _xp_cache: dict = field(default_factory=dict, repr=False)

    # -- battle -------------------------------------------------------------------------
    def raw_odds(self, player_level: float, enemy_level: float) -> float:
        return self.battle.eval({
            "playerLevel": float(player_level), "enemyLevel": float(enemy_level),
            "delta": float(player_level) - float(enemy_level),
        })

    def odds(self, player_level: float, enemy_level: int) -> float:
        key = (round(player_level, 6), enemy_level)
        p = self._odds_cache.get(key)
        if p is None:
            p = self.raw_odds(player_level, enemy_level)
            p = 0.0 if p < 0 else 1.0 if p > 1 else p
            self._odds_cache[key] = p
        return p

    # -- progression ---------------------------------------------------------------------
    def xp_to_next(self, level: int) -> int:
        if level >= self.max_level:
            return 0
        return self.xp_table[level]

    def xp_gain(self, enemy_xp: float, player_level: int, enemy_level: int) -> int:
        key = (enemy_xp, player_level, enemy_level)
        v = self._xp_cache.get(key)
        if v is None:
            raw = self.xp_gain_formula.eval({
                "xp": float(enemy_xp), "playerLevel": float(player_level),
                "enemyLevel": float(enemy_level), "delta": float(player_level - enemy_level),
            })
            v = max(0, int(math.floor(raw + 1e-9)))
            self._xp_cache[key] = v
        return v

    def total_xp_to(self, level: int) -> int:
        return sum(self.xp_table[l] for l in range(1, min(level, self.max_level)))

    # -- goods ---------------------------------------------------------------------------
    def is_currency(self, gid: str) -> bool:
        return gid in self.currencies

    def fmt(self, bundle: Bundle | None, empty: str = "free") -> str:
        return self.money.format(bundle, empty=empty)

    def fmt_amount(self, cid: str, amount: float) -> str:
        return self.currencies[cid].format(amount)

    def fmt_good(self, gid: str, qty: float) -> str:
        if gid in self.currencies:
            return self.currencies[gid].format(qty)
        q = int(qty) if float(qty).is_integer() else qty
        return f"{q}× {gid}"

    def gear_slots(self) -> dict[str, list[Item]]:
        slots: dict[str, list[Item]] = {}
        for it in self.items.values():
            if it.slot:
                slots.setdefault(it.slot, []).append(it)
        for v in slots.values():
            v.sort(key=lambda i: (i.min_level, i.power))
        return slots

    def best_loadout(self, level: int, pool: set[str] | None = None) -> dict[str, Item]:
        """Best gear per slot usable at `level` (optionally restricted to item ids in `pool`)."""
        out: dict[str, Item] = {}
        for slot, items in self.gear_slots().items():
            best = None
            for it in items:
                if it.min_level > level or (pool is not None and it.id not in pool):
                    continue
                if best is None or it.power > best.power:
                    best = it
            if best is not None and best.power > 0:
                out[slot] = best
        return out

    def loadout_power(self, loadout: dict[str, Item]) -> float:
        return sum(i.power for i in loadout.values())

    def locate(self, path) -> tuple[str | None, int | None]:
        return self.source.locate(path)

    def location_order(self) -> list[Location]:
        """Combat locations sorted by intended level."""
        locs = [l for l in self.locations.values() if not l.safe and l.variants]
        return sorted(locs, key=lambda l: (l.level_range or (10**9, 10**9), l.id))

    def sell_value(self, item_id: str, cid: str | None = None) -> int:
        it = self.items.get(item_id)
        if not it or not it.sell:
            return 0
        return it.sell.get(cid or self.primary, 0)

    def buy_price(self, item_id: str, cid: str | None = None) -> int | None:
        it = self.items.get(item_id)
        if not it or it.buy is None:
            return None
        return it.buy.get(cid or self.primary, 0)


# ----------------------------------------------------------------------------------------
# compiler
# ----------------------------------------------------------------------------------------

class _Compiler:
    def __init__(self, res: LoadResult):
        self.res = res
        self.d: EconomyDef = res.model  # type: ignore[assignment]
        self.findings: list[Finding] = []

    def err(self, code: str, path, message: str, fix: str = "", severity: str = "error"):
        p = fmt_path(path)
        file, line = self.res.source.locate(path if not isinstance(path, str) else p)
        self.findings.append(Finding(code, severity, message, path=p, fix=fix, file=file, line=line))

    def ref(self, kind: str, ident: str, pool, path) -> bool:
        if ident in pool:
            return True
        close = difflib.get_close_matches(ident, list(pool), n=1, cutoff=0.6)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        self.err("unknown-reference", path, f"unknown {kind} '{ident}'{hint}",
                 fix=f"Defined {kind}s: {', '.join(sorted(pool)) or 'none'}")
        return False

    def formula(self, src, allowed, path, what, params=None) -> Formula | None:
        try:
            return Formula.compile(src, allowed, params if params is not None else self.params, what=what)
        except FormulaError as e:
            self.err("bad-formula", path, str(e))
            return None

    def money(self, value, path, default=None, allow_none=True) -> Bundle | None:
        if value is None:
            return None if allow_none else {}
        try:
            return self.parser.parse(value, default=default)
        except MoneyError as e:
            self.err("bad-money", path, f"{e}")
            return None

    def int_range(self, value, path, default: tuple[int, int] | None, lo: int = 0) -> tuple[int, int] | None:
        if value is None:
            return default
        if isinstance(value, bool):
            self.err("bad-value", path, f"expected an integer or [min, max], got {value!r}")
            return None
        if isinstance(value, (int, float)) and float(value).is_integer():
            v = int(value)
            r = (v, v)
        elif isinstance(value, list) and len(value) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and float(x).is_integer() for x in value):
            r = (int(value[0]), int(value[1]))
        else:
            self.err("bad-value", path, f"expected an integer or [min, max] of integers, got {value!r}")
            return None
        if r[0] > r[1]:
            self.err("bad-value", path, f"range {list(r)} has min > max")
            return None
        if r[0] < lo:
            self.err("bad-value", path, f"range {list(r)} must be ≥ {lo}")
            return None
        return r

    def target(self, value, path) -> tuple[float | None, float | None] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            lo, hi = value.get("min"), value.get("max")
            extra = set(value) - {"min", "max"}
            if extra:
                self.err("schema", path, f"unknown keys {sorted(extra)} (use min/max)")
                return None
        elif isinstance(value, list) and len(value) == 2:
            lo, hi = value
        else:
            self.err("bad-value", path, f"expected [min, max] or {{min, max}}, got {value!r}")
            return None
        for v in (lo, hi):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                self.err("bad-value", path, f"target bounds must be numbers, got {v!r}")
                return None
        if lo is not None and hi is not None and lo > hi:
            self.err("bad-value", path, f"target {value!r} has min > max")
            return None
        return (lo, hi)

    # ------------------------------------------------------------------------------------
    def run(self) -> Economy | None:
        d = self.d
        self.params = dict(d.params)

        # currencies --------------------------------------------------------------------
        currencies: dict[str, Currency] = {}
        unit_owner: dict[str, str] = {}
        for cid, c in d.currencies.items():
            dens = []
            for i, den in enumerate(c.denominations):
                sym = den.symbol or den.name
                dens.append(Denomination(den.name, sym, den.value))
                for unit in {sym, den.name}:
                    owner = unit_owner.get(unit)
                    if owner and owner != cid:
                        self.err("duplicate-definition", ("currencies", cid, "denominations", i),
                                 f"denomination '{unit}' is also used by currency '{owner}'")
                    unit_owner[unit] = cid
            if dens:
                values = [x.value for x in dens]
                if 1 not in values:
                    self.err("bad-value", ("currencies", cid, "denominations"),
                             "one denomination must have value 1 (the base unit)")
                if len(set(values)) != len(values):
                    self.err("bad-value", ("currencies", cid, "denominations"), "denomination values must be distinct")
            dens.sort(key=lambda x: x.value)
            currencies[cid] = Currency(cid, dens, None, c.primary, c.description or "")
        for cid in currencies:
            owner = unit_owner.get(cid)
            if owner and owner != cid:
                self.err("duplicate-definition", ("currencies", cid),
                         f"currency id '{cid}' collides with a denomination of '{owner}'")
        if not currencies:
            self.err("missing-section", ("currencies",), "at least one currency is required")
            return None
        primaries = [cid for cid, c in currencies.items() if c.primary]
        if len(primaries) > 1:
            self.err("bad-value", ("currencies", primaries[1], "primary"), "only one currency can be primary")
        primary = primaries[0] if primaries else next(iter(currencies))
        currencies[primary].primary = True
        self.parser = MoneyParser(currencies, primary)
        for cid, c in d.currencies.items():
            if c.wallet_cap is not None:
                cap = self.money(c.wallet_cap, ("currencies", cid, "wallet_cap"), default=cid)
                if cap is not None:
                    if set(cap) != {cid}:
                        self.err("bad-money", ("currencies", cid, "wallet_cap"), f"wallet_cap must be in '{cid}'")
                    else:
                        currencies[cid].cap = cap[cid]

        goods_pool = set(currencies) | set(d.items)
        for iid in d.items:
            if iid in currencies:
                self.err("duplicate-definition", ("items", iid), f"'{iid}' is both an item and a currency")

        # items ---------------------------------------------------------------------------
        items: dict[str, Item] = {}
        for iid, it in d.items.items():
            p = ("items", iid)
            buy = self.money(it.buy, p + ("buy",))
            sell = self.money(it.sell, p + ("sell",))
            half = it.half_life
            items[iid] = Item(iid, buy, sell, it.slot, float(it.power), half, it.min_level,
                              it.consumable, list(it.tags), it.description or "")
            if it.consumable and it.slot:
                self.err("bad-value", p, "an item cannot be both consumable and equippable (slot)")

        # recipes -------------------------------------------------------------------------
        recipes: dict[str, Recipe] = {}
        for rid, r in d.recipes.items():
            p = ("recipes", rid)
            ok = True
            io: dict[str, dict[str, int]] = {}
            for side in ("inputs", "outputs"):
                out: dict[str, int] = {}
                for gid, q in getattr(r, side).items():
                    gp = p + (side, gid)
                    if not self.ref("item or currency", gid, goods_pool, gp):
                        ok = False
                        continue
                    if gid in currencies:
                        b = self.money(q, gp, default=gid)
                        if b is None:
                            ok = False
                            continue
                        for cid, amt in b.items():
                            out[cid] = out.get(cid, 0) + amt
                    else:
                        if isinstance(q, bool) or not isinstance(q, (int, float)) or not float(q).is_integer() or q <= 0:
                            self.err("bad-value", gp, f"item quantity must be a positive integer, got {q!r}")
                            ok = False
                            continue
                        out[gid] = out.get(gid, 0) + int(q)
                io[side] = out
            cost = self.money(r.cost, p + ("cost",)) or {}
            if not ok:
                continue
            if not io["outputs"]:
                self.err("bad-value", p + ("outputs",), "recipe produces nothing")
                continue
            recipes[rid] = Recipe(rid, io["inputs"], io["outputs"], cost,
                                  Fraction(str(r.success_chance)), r.min_level, r.description or "")

        # enemies -------------------------------------------------------------------------
        enemies: dict[str, Enemy] = {}
        for eid, e in d.enemies.items():
            p = ("enemies", eid)
            drops: list[Drop] = []
            for i, dr in enumerate(e.drops):
                dp = p + ("drops", i)
                if (dr.item is None) == (dr.currency is None):
                    self.err("bad-value", dp, "a drop needs exactly one of `item` or `currency`")
                    continue
                if dr.item is not None:
                    if not self.ref("item", dr.item, d.items, dp + ("item",)):
                        continue
                    if dr.amount is not None:
                        self.err("schema", dp + ("amount",), "item drops use `qty`, currency drops use `amount`")
                        continue
                    r = self.int_range(dr.qty, dp + ("qty",), (1, 1), lo=0)
                    if r is None:
                        continue
                    drops.append(Drop("item", dr.item, r[0], r[1], dr.chance))
                else:
                    if not self.ref("currency", dr.currency, currencies, dp + ("currency",)):
                        continue
                    if dr.qty is not None:
                        self.err("schema", dp + ("qty",), "currency drops use `amount`, item drops use `qty`")
                        continue
                    val = dr.amount
                    if val is None:
                        self.err("bad-value", dp, "currency drop needs an `amount`")
                        continue
                    if isinstance(val, list):
                        if len(val) != 2:
                            self.err("bad-value", dp + ("amount",), "amount range must be [min, max]")
                            continue
                        lo_b = self.money(val[0], dp + ("amount", 0), default=dr.currency)
                        hi_b = self.money(val[1], dp + ("amount", 1), default=dr.currency)
                        if lo_b is None or hi_b is None:
                            continue
                        lo, hi = lo_b.get(dr.currency, 0), hi_b.get(dr.currency, 0)
                        if set(lo_b) - {dr.currency} or set(hi_b) - {dr.currency}:
                            self.err("bad-money", dp + ("amount",), f"amount must be in '{dr.currency}'")
                            continue
                    else:
                        b = self.money(val, dp + ("amount",), default=dr.currency)
                        if b is None:
                            continue
                        if set(b) - {dr.currency}:
                            self.err("bad-money", dp + ("amount",), f"amount must be in '{dr.currency}'")
                            continue
                        lo = hi = b.get(dr.currency, 0)
                    if lo > hi:
                        self.err("bad-value", dp + ("amount",), "amount range has min > max")
                        continue
                    drops.append(Drop("currency", dr.currency, lo, hi, dr.chance))
            enemies[eid] = Enemy(eid, e.level, float(e.xp), drops, e.description or "")

        # locations -----------------------------------------------------------------------
        locations: dict[str, Location] = {}
        rate = d.meta.encounter_rate
        for lid, loc in d.locations.items():
            p = ("locations", lid)
            lr = self.int_range(loc.level_range, p + ("level_range",), None, lo=1)
            if lr is not None and lr[0] > d.progression.max_level:
                self.err("bad-value", p + ("level_range",),
                         f"level_range {list(lr)} starts above progression.max_level ({d.progression.max_level})")
                lr = None
            encs: list[Encounter] = []
            for i, en in enumerate(loc.enemies):
                ep = p + ("enemies", i)
                if not self.ref("enemy", en.enemy, d.enemies, ep + ("enemy",)):
                    continue
                base = d.enemies[en.enemy].level
                r = self.int_range(en.level, ep + ("level",), (base, base), lo=1)
                if r is None:
                    continue
                encs.append(Encounter(en.enemy, en.weight, r[0], r[1]))
            if loc.safe and loc.enemies:
                self.err("bad-value", p + ("safe",), "a safe location cannot have enemies")
            if not loc.safe and not loc.enemies:
                self.err("bad-value", p, "location has no enemies (mark it `safe: true` if it is a town)")
            L = Location(lid, lr, encs, loc.encounter_rate or rate, loc.safe, loc.description or "")
            if encs:
                L.build_variants()
            locations[lid] = L

        # battle / progression ------------------------------------------------------------
        bparams = dict(self.params)
        bparams.update(d.battle.params)
        battle = self.formula(d.battle.formula, BATTLE_VARS, ("battle", "formula"), "battle.formula", bparams)
        prog = d.progression
        max_level = prog.max_level
        xp_table = [0] * (max_level + 1)
        if isinstance(prog.xp_to_next, list):
            if len(prog.xp_to_next) < max_level - 1:
                self.err("bad-value", ("progression", "xp_to_next"),
                         f"list has {len(prog.xp_to_next)} entries but max_level {max_level} needs {max_level - 1}")
            for lvl in range(1, max_level):
                if lvl - 1 < len(prog.xp_to_next):
                    v = prog.xp_to_next[lvl - 1]
                    if v < 1:
                        self.err("bad-value", ("progression", "xp_to_next", lvl - 1), "XP per level must be ≥ 1")
                    xp_table[lvl] = max(1, int(v))
                else:
                    xp_table[lvl] = max(1, xp_table[lvl - 1] if lvl > 1 else 1)
        else:
            f = self.formula(prog.xp_to_next, XP_TO_NEXT_VARS, ("progression", "xp_to_next"), "progression.xp_to_next")
            if f is not None:
                for lvl in range(1, max_level):
                    try:
                        v = f.eval({"level": float(lvl)})
                    except FormulaError as e:
                        self.err("bad-formula", ("progression", "xp_to_next"), str(e))
                        break
                    if v < 1:
                        self.err("bad-value", ("progression", "xp_to_next"),
                                 f"XP to reach level {lvl + 1} is {v:g}; must be ≥ 1")
                        v = 1
                    xp_table[lvl] = int(math.floor(v + 1e-9))
        xp_gain = self.formula(prog.xp_gain, XP_GAIN_VARS, ("progression", "xp_gain"), "progression.xp_gain")

        # respawn -------------------------------------------------------------------------
        rs = d.respawn
        rp = ("respawn",)
        if rs.location is not None:
            self.ref("location", rs.location, d.locations, rp + ("location",))
        r_level = self.formula(rs.level, RESPAWN_LEVEL_VARS, rp + ("level",), "respawn.level")
        r_xp = self.formula(rs.xp, RESPAWN_XP_VARS, rp + ("xp",), "respawn.xp")
        r_cur: dict[str, Formula] = {}
        for cid, src in rs.currencies.items():
            if self.ref("currency", cid, currencies, rp + ("currencies", cid)):
                f = self.formula(src, RESPAWN_CURRENCY_VARS, rp + ("currencies", cid), f"respawn.currencies.{cid}")
                if f is not None:
                    r_cur[cid] = f
        for part in ("inventory", "equipped"):
            for i, iid in enumerate(getattr(rs, part).protect):
                self.ref("item", iid, d.items, rp + (part, "protect", i))
        grants: list[Grant] = []
        for i, g in enumerate(rs.grants):
            gp = rp + ("grants", i)
            if (g.item is None) == (g.currency is None):
                self.err("bad-value", gp, "a grant needs exactly one of `item` or `currency`")
                continue
            if g.item is not None:
                if self.ref("item", g.item, d.items, gp + ("item",)):
                    grants.append(Grant("item", g.item, g.qty or 1, g.if_missing))
            else:
                if self.ref("currency", g.currency, currencies, gp + ("currency",)):
                    b = self.money(g.amount, gp + ("amount",), default=g.currency)
                    if b:
                        grants.append(Grant("currency", g.currency, b.get(g.currency, 0), g.if_missing))
        respawn = None
        if r_level is not None and r_xp is not None:
            respawn = Respawn(rs.location, r_level, r_xp, r_cur, rs.inventory.lose_chance,
                              set(rs.inventory.protect), rs.equipped.lose_chance, set(rs.equipped.protect), grants)

        # player --------------------------------------------------------------------------
        st = d.player.start
        sp = ("player", "start")
        if st.level > max_level:
            self.err("bad-value", sp + ("level",), f"start level {st.level} exceeds max_level {max_level}")
        loc_id = st.location
        if loc_id is not None:
            self.ref("location", loc_id, d.locations, sp + ("location",))
        else:
            combat = [l for l in locations.values() if not l.safe and l.variants]
            combat.sort(key=lambda l: (l.level_range or (10**9, 10**9))[0])
            loc_id = combat[0].id if combat else (next(iter(locations)) if locations else "")
        s_cur: Bundle = {}
        for cid, v in st.currencies.items():
            if self.ref("currency", cid, currencies, sp + ("currencies", cid)):
                b = self.money(v, sp + ("currencies", cid), default=cid)
                if b:
                    s_cur[cid] = s_cur.get(cid, 0) + b.get(cid, 0)
        s_items: dict[str, int] = {}
        for iid, q in st.items.items():
            if self.ref("item", iid, d.items, sp + ("items", iid)):
                if q < 0:
                    self.err("bad-value", sp + ("items", iid), "quantity must be ≥ 0")
                s_items[iid] = max(0, q)
        if st.equipped is not None:
            for i, iid in enumerate(st.equipped):
                if self.ref("item", iid, d.items, sp + ("equipped", i)):
                    if not d.items[iid].slot:
                        self.err("bad-value", sp + ("equipped", i), f"'{iid}' has no slot and cannot be equipped")
                    if s_items.get(iid, 0) == 0:
                        s_items[iid] = 1
        start = Start(st.level, st.xp, loc_id or "", s_cur, s_items, st.equipped)

        pol = d.player.policy
        pp = ("player", "policy")
        if pol.location != "auto":
            self.ref("location", pol.location, d.locations, pp + ("location",))
        reserve = self.money(pol.reserve, pp + ("reserve",)) or {}
        policy = Policy(pol.location, pol.max_death_risk, pol.objective, pol.sell_loot, pol.sell_outdated_gear,
                        pol.buy_gear, pol.buy_consumables, pol.use_consumables_below, pol.keep_consumables,
                        pol.craft, reserve, pol.shop_every)

        tg = d.targets
        targets = Targets(
            lifespan=self.target(tg.lifespan, ("targets", "lifespan")),
            even_fight_win=self.target(tg.even_fight_win, ("targets", "even_fight_win")),
            encounters_per_level=self.target(tg.encounters_per_level, ("targets", "encounters_per_level")),
            hours_to_max_level=self.target(tg.hours_to_max_level, ("targets", "hours_to_max_level")),
            max_upkeep_ratio=tg.max_upkeep_ratio,
            wallet_fill_encounters=tg.wallet_fill_encounters,
        )
        ignore = [IgnoreRule(x) if isinstance(x, str) else IgnoreRule(x.code, x.path) for x in d.lint.ignore]

        if battle is None or xp_gain is None or respawn is None:
            return None
        eco = Economy(
            name=d.meta.name, encounter_rate=rate, currencies=currencies, primary=primary,
            items=items, recipes=recipes, enemies=enemies, locations=locations, battle=battle,
            max_level=max_level, xp_table=xp_table, xp_gain_formula=xp_gain, respawn=respawn,
            start=start, policy=policy, targets=targets, ignore=ignore, source=self.res.source,
            files=self.res.files, money=self.parser,
        )
        self._smoke_test(eco)
        return eco

    def _smoke_test(self, eco: Economy):
        """Evaluate formulas on representative inputs so runtime errors surface at lint time."""
        levels = sorted({v.level for l in eco.locations.values() for v in l.variants} | {1, eco.max_level})
        try:
            for pl in range(1, eco.max_level + 1):
                for el in levels:
                    eco.raw_odds(pl, el)
        except FormulaError as e:
            self.err("bad-formula", ("battle", "formula"), str(e))
        try:
            for e in eco.enemies.values():
                for pl in (1, eco.max_level):
                    eco.xp_gain(e.xp, pl, e.level)
        except FormulaError as e:
            self.err("bad-formula", ("progression", "xp_gain"), str(e))
        r = eco.respawn
        try:
            for lvl in (1, eco.max_level):
                r.level.eval({"level": float(lvl), "xp": 0.0, "deaths": 1.0})
                r.xp.eval({"level": float(lvl), "xp": 0.0, "deaths": 1.0, "xpToNext": float(eco.xp_to_next(lvl) or 1)})
        except FormulaError as e:
            self.err("bad-formula", ("respawn",), str(e))
        for cid, f in r.currencies.items():
            cap = eco.currencies[cid].cap or 10**6
            try:
                for amt in (0, 1, 10, cap):
                    f.eval({"amount": float(amt), "cap": float(cap), "level": 1.0, "deaths": 1.0})
            except FormulaError as e:
                self.err("bad-formula", ("respawn", "currencies", cid), str(e))


def compile_economy(res: LoadResult) -> tuple[Economy | None, list[Finding]]:
    if res.model is None:
        return None, []
    c = _Compiler(res)
    eco = c.run()
    if any(f.severity == "error" for f in c.findings):
        return None, c.findings
    return eco, c.findings


def load_economy(path) -> tuple[Economy | None, list[Finding], LoadResult]:
    """Load + validate + compile. Returns (economy or None, findings, raw load result)."""
    from .loader import load_path
    res = load_path(path)
    findings = list(res.findings)
    if not res.ok:
        return None, findings, res
    eco, more = compile_economy(res)
    findings.extend(more)
    return eco, findings, res
