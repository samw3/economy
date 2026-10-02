"""Market analysis: money pumps, duplication loops and shadow prices.

Every vendor trade and recipe is a linear, repeatable *action* with a fixed net change of
goods. By Stiemke's lemma exactly one of these holds:

1. there are shadow prices p ≥ 0, strictly positive on protected goods, such that no action
   increases value (a *certificate* that no exploit exists), or
2. some non-negative integer combination of actions consumes nothing on net and produces a
   protected good (a *pump*).

We find (1) or (2) with linear programming (HiGHS), then recover an exact rational solution and
verify it with exact arithmetic. Lean re-checks the same certificate or pump in its kernel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from functools import reduce

import numpy as np
from scipy.optimize import linprog

from ..economy import Economy


@dataclass
class Action:
    id: str                       # e.g. "buy iron_ore"
    kind: str                     # buy | sell | craft
    ref: str                      # item id (buy/sell) or recipe id (craft)
    delta: dict[str, Fraction]    # good -> expected net change per execution (never contains zeros)
    path: str                     # source path of the knob that sets this action's rates
    consumes: dict[str, Fraction] = field(default_factory=dict)  # gross amounts used up per execution
    chancy: bool = False          # a recipe with success_chance < 1 (delta is the expected value)

    def int_delta(self) -> tuple[int, dict[str, int]]:
        """(scale, integer delta) with scale the smallest positive integer making delta integral."""
        den = reduce(math.lcm, (v.denominator for v in self.delta.values()), 1)
        return den, {g: int(v * den) for g, v in self.delta.items() if v != 0}


@dataclass
class Market:
    goods: list[str]
    index: dict[str, int]
    actions: list[Action]
    protected: set[str]

    def matrix(self) -> np.ndarray:
        m = np.zeros((len(self.actions), len(self.goods)))
        for i, a in enumerate(self.actions):
            for g, v in a.delta.items():
                m[i, self.index[g]] += float(v)
        return m


@dataclass
class Pump:
    combo: list[tuple[int, int]]     # (times, action index), integer multiplicities
    net: dict[str, int]              # net gain per loop, by good (only non-zero)
    kind: str                        # "money-pump" | "duplication" | "free-item" | "buy-below-sell"
    capital: dict[str, int] = field(default_factory=dict)  # goods needed up front
    explanation: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)


@dataclass
class MarketResult:
    market: Market
    prices: list[Fraction] | None = None     # exact certificate, if no pump
    pumps: list[Pump] = field(default_factory=list)
    inconclusive: str | None = None

    def shadow_price(self, good: str, eco: Economy) -> float | None:
        if self.prices is None:
            return None
        pc = self.prices[self.market.index[eco.primary]]
        if pc == 0:
            return None
        return float(self.prices[self.market.index[good]] / pc)


# ----------------------------------------------------------------------------------------

def valuable_goods(eco: Economy) -> set[str]:
    """Currencies plus items that are worth something: sellable, useful in battle, or (transitively)
    an ingredient of something valuable."""
    val = set(eco.currencies)
    for it in eco.items.values():
        if (it.sell and any(v > 0 for v in it.sell.values())) or it.power > 0:
            val.add(it.id)
    changed = True
    while changed:
        changed = False
        for r in eco.recipes.values():
            if any(o in val for o in r.outputs):
                for i in r.inputs:
                    if i not in val:
                        val.add(i)
                        changed = True
    return val


def _nz(d: dict[str, Fraction]) -> dict[str, Fraction]:
    return {g: v for g, v in d.items() if v != 0}


def build_market(eco: Economy, lucky: bool = False) -> Market:
    """All repeatable market actions. Recipes with a success chance produce their *expected* output,
    the right model for exploits that pay off on average. With `lucky=True` they always succeed:
    the model behind "whatever the luck" claims (income ceiling, fastest acquisition)."""
    goods = list(eco.currencies) + list(eco.items)
    index = {g: i for i, g in enumerate(goods)}
    actions: list[Action] = []
    for it in eco.items.values():
        if it.buy is not None:
            d = {cid: Fraction(-amt) for cid, amt in it.buy.items()}
            d[it.id] = d.get(it.id, Fraction(0)) + 1
            actions.append(Action(f"buy {it.id}", "buy", it.id, _nz(d), f"items.{it.id}.buy",
                                  _nz({cid: Fraction(amt) for cid, amt in it.buy.items()})))
        if it.sell is not None:
            d = {cid: Fraction(amt) for cid, amt in it.sell.items()}
            d[it.id] = d.get(it.id, Fraction(0)) - 1
            actions.append(Action(f"sell {it.id}", "sell", it.id, _nz(d), f"items.{it.id}.sell",
                                  {it.id: Fraction(1)}))
    for r in eco.recipes.values():
        d: dict[str, Fraction] = {}
        used: dict[str, Fraction] = {}
        for g, q in list(r.inputs.items()) + list(r.cost.items()):
            d[g] = d.get(g, Fraction(0)) - q
            used[g] = used.get(g, Fraction(0)) + q
        rate = Fraction(1) if lucky else r.success
        for g, q in r.outputs.items():
            d[g] = d.get(g, Fraction(0)) + rate * q
        actions.append(Action(f"craft {r.id}", "craft", r.id, _nz(d), f"recipes.{r.id}", _nz(used),
                              chancy=r.success < 1 and not lucky))
    return Market(goods, index, actions, valuable_goods(eco))


def has_chancy_recipes(eco: Economy) -> bool:
    return any(r.success < 1 for r in eco.recipes.values())


# ----------------------------------------------------------------------------------------
# exact linear algebra helpers

def _rationalize(x: float, limit: int) -> Fraction:
    return Fraction(x).limit_denominator(limit)


def _solve_exact(eqs: list[tuple[dict[int, Fraction], Fraction]], n: int, guess: list[Fraction]) -> list[Fraction] | None:
    """Solve sparse linear equations exactly; free variables are fixed to `guess`."""
    rows = [({k: v for k, v in c.items() if v != 0}, b) for c, b in eqs]
    pivots: list[tuple[int, dict[int, Fraction], Fraction]] = []
    for coeffs, rhs in rows:
        # eliminate existing pivots
        for col, pc, pb in pivots:
            f = coeffs.get(col)
            if f:
                for k, v in pc.items():
                    nv = coeffs.get(k, Fraction(0)) - f * v
                    if nv:
                        coeffs[k] = nv
                    else:
                        coeffs.pop(k, None)
                rhs = rhs - f * pb
        if not coeffs:
            if rhs != 0:
                return None
            continue
        col = min(coeffs)
        piv = coeffs[col]
        coeffs = {k: v / piv for k, v in coeffs.items()}
        rhs = rhs / piv
        # back-eliminate this column from previous pivots
        new_pivots = []
        for pcol, pc, pb in pivots:
            f = pc.get(col)
            if f:
                pc = dict(pc)
                for k, v in coeffs.items():
                    nv = pc.get(k, Fraction(0)) - f * v
                    if nv:
                        pc[k] = nv
                    else:
                        pc.pop(k, None)
                pb = pb - f * rhs
            new_pivots.append((pcol, pc, pb))
        pivots = new_pivots + [(col, coeffs, rhs)]
    x = list(guess)
    pivot_cols = {c for c, _, _ in pivots}
    for col, pc, pb in pivots:
        val = pb
        for k, v in pc.items():
            if k != col:
                if k in pivot_cols:
                    continue
                val -= v * x[k]
        x[col] = val
    # pivot rows may reference other pivot columns only if elimination was incomplete; resolve
    for _ in range(len(pivots)):
        changed = False
        for col, pc, pb in pivots:
            val = pb - sum(v * x[k] for k, v in pc.items() if k != col)
            if val != x[col]:
                x[col] = val
                changed = True
        if not changed:
            break
    return x


# ----------------------------------------------------------------------------------------

def _certificate(mk: Market) -> list[Fraction] | None:
    n = len(mk.goods)
    rows = [a.delta for a in mk.actions]
    if not rows:
        return [Fraction(1)] * n
    A = mk.matrix()
    bounds = [(1, None) if g in mk.protected else (0, None) for g in mk.goods]
    res = linprog(c=np.ones(n), A_ub=A, b_ub=np.zeros(len(rows)), bounds=bounds, method="highs")
    if res.status != 0:
        return None
    x = res.x

    def check(p: list[Fraction]) -> bool:
        for i, g in enumerate(mk.goods):
            if p[i] < 0 or (g in mk.protected and p[i] <= 0):
                return False
        for a in mk.actions:
            if sum(v * p[mk.index[g]] for g, v in a.delta.items()) > 0:
                return False
        return True

    for limit in (10**3, 10**6, 10**9):
        p = [_rationalize(v, limit) for v in x]
        if check(p):
            return p
    # crossover: make tight constraints exactly tight
    tol = 1e-7 * max(1.0, float(np.max(np.abs(x))))
    eqs = []
    for a in mk.actions:
        val = sum(float(v) * x[mk.index[g]] for g, v in a.delta.items())
        if abs(val) <= tol:
            eqs.append(({mk.index[g]: v for g, v in a.delta.items()}, Fraction(0)))
    for i, g in enumerate(mk.goods):
        lb = 1 if g in mk.protected else 0
        if abs(x[i] - lb) <= tol:
            eqs.append(({i: Fraction(1)}, Fraction(lb)))
    p = _solve_exact(eqs, n, [_rationalize(v, 10**6) for v in x])
    if p is not None and check(p):
        return p
    return None


def _find_pump(mk: Market, targets: set[str], banned: set[int]) -> list[Fraction] | None:
    """Minimise total actions subject to net ≥ 0 and Σ_{targets} net ≥ 1."""
    m, n = len(mk.actions), len(mk.goods)
    if m == 0 or not targets:
        return None
    A = mk.matrix()  # m × n
    # constraints: -A^T x ≤ 0 (net ≥ 0) ; -Σ_t net_t ≤ -1
    tgt = np.array([1.0 if g in targets else 0.0 for g in mk.goods])
    A_ub = np.vstack([-A.T, -(A @ tgt)[None, :]])
    b_ub = np.concatenate([np.zeros(n), [-1.0]])
    bounds = [(0, 0) if i in banned else (0, None) for i in range(m)]
    res = linprog(c=np.ones(m), A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if res.status != 0:
        return None
    x = res.x

    def check(xs: list[Fraction]) -> bool:
        if any(v < 0 for v in xs):
            return False
        net = _net(mk, xs)
        return all(v >= 0 for v in net.values()) and any(net.get(g, 0) > 0 for g in targets)

    for limit in (10**3, 10**6):
        xs = [_rationalize(v, limit) if v > 1e-9 else Fraction(0) for v in x]
        if check(xs):
            return xs
    tol = 1e-7
    eqs = []
    net_f = A.T @ x
    for gi, g in enumerate(mk.goods):
        if abs(net_f[gi]) <= tol:
            eqs.append(({ai: v for ai, a in enumerate(mk.actions) for gg, v in a.delta.items() if gg == g}, Fraction(0)))
    for ai in range(m):
        if x[ai] <= tol:
            eqs.append(({ai: Fraction(1)}, Fraction(0)))
    xs = _solve_exact(eqs, m, [_rationalize(v, 10**6) for v in x])
    if xs is not None and check(xs):
        return xs
    return None


def _net(mk: Market, xs: list[Fraction]) -> dict[str, Fraction]:
    net: dict[str, Fraction] = {}
    for ai, k in enumerate(xs):
        if k:
            for g, v in mk.actions[ai].delta.items():
                net[g] = net.get(g, Fraction(0)) + k * v
    return {g: v for g, v in net.items() if v != 0}


def _integerize(xs: list[Fraction]) -> list[tuple[int, int]]:
    den = reduce(math.lcm, (x.denominator for x in xs if x), 1)
    ints = [int(x * den) for x in xs]
    g = reduce(math.gcd, (v for v in ints if v), 0) or 1
    return [(v // g, i) for i, v in enumerate(ints) if v]


def analyze_market(eco: Economy, max_pumps: int = 5) -> MarketResult:
    mk = build_market(eco)
    res = MarketResult(mk)
    p = _certificate(mk)
    if p is not None:
        res.prices = p
        return res
    # find pumps: money first, then items
    seen: set[frozenset[int]] = set()
    currencies = set(eco.currencies)
    items = mk.protected - currencies

    def collect(targets: set[str], banned: set[int], depth: int):
        if len(res.pumps) >= max_pumps:
            return
        xs = _find_pump(mk, targets, banned)
        if xs is None:
            return
        combo = _integerize(xs)
        support = frozenset(i for _, i in combo)
        if support not in seen:
            seen.add(support)
            res.pumps.append(_describe(eco, mk, combo))
        if depth > 0:
            for _, i in combo:
                collect(targets, banned | {i}, depth - 1)

    collect(currencies, set(), 2)
    collect(items, set(), 1)
    if not res.pumps:
        res.inconclusive = ("no shadow-price certificate exists but no pump could be extracted "
                            "(numerical difficulty)")
    return res


# ----------------------------------------------------------------------------------------
# explanations

_KIND_ORDER = {"buy": 0, "craft": 1, "sell": 2}


def _describe(eco: Economy, mk: Market, combo: list[tuple[int, int]]) -> Pump:
    # integer net per loop (scale so that expected values are whole numbers)
    xs = [Fraction(0)] * len(mk.actions)
    for k, i in combo:
        xs[i] = Fraction(k)
    net = _net(mk, xs)
    den = reduce(math.lcm, (v.denominator for v in net.values()), 1)
    if den != 1:
        combo = [(k * den, i) for k, i in combo]
        net = {g: v * den for g, v in net.items()}
    net_i = {g: int(v) for g, v in net.items()}

    combo = sorted(combo, key=lambda t: (_KIND_ORDER[mk.actions[t[1]].kind], mk.actions[t[1]].id))
    acts = combo
    kinds = {mk.actions[i].kind for _, i in combo}
    money_gain = {g: v for g, v in net_i.items() if g in eco.currencies and v > 0}
    if money_gain and kinds == {"buy", "sell"} and len({mk.actions[i].ref for _, i in combo}) == 1:
        kind = "buy-below-sell"
    elif money_gain:
        kind = "money-pump"
    elif kinds == {"buy"}:
        kind = "free-item"
    else:
        kind = "duplication"

    # capital needed: run buys -> crafts -> sells one execution at a time (inputs are used up
    # before outputs appear), tracking the lowest balance of each good
    bal: dict[str, Fraction] = {}
    low: dict[str, Fraction] = {}
    for k, i in acts:
        a = mk.actions[i]
        for _ in range(min(k, 10_000)):
            for g, v in a.consumes.items():
                bal[g] = bal.get(g, Fraction(0)) - v
                low[g] = min(low.get(g, Fraction(0)), bal[g])
            for g, v in a.delta.items():
                bal[g] = bal.get(g, Fraction(0)) + v + a.consumes.get(g, Fraction(0))
    capital = {g: int(math.ceil(-v)) for g, v in low.items() if v < 0}

    lines = []
    for k, i in acts:
        a = mk.actions[i]
        parts = []
        for g, v in sorted(a.delta.items(), key=lambda t: (t[1] > 0, t[0])):
            amt = v * k
            sign = "+" if amt > 0 else "−"
            parts.append(f"{sign}{eco.fmt_good(g, float(abs(amt)))}")
        lines.append(f"{k}× {a.id}: " + ", ".join(parts))
    gain = ", ".join(f"+{eco.fmt_good(g, v)}" for g, v in net_i.items() if v > 0)
    chancy = any(mk.actions[i].chancy for _, i in combo)
    lines.append(f"net per loop: {gain}" + (" (expected value: a recipe in the loop has a success chance)" if chancy else ""))
    if capital:
        lines.append("working capital: " + ", ".join(eco.fmt_good(g, v) for g, v in capital.items()))

    fixes = []
    for cid, G in money_gain.items():
        for k, i in acts:
            a = mk.actions[i]
            step = -(-G // k)  # ceil(G / k)
            if a.kind == "buy" and cid in (eco.items[a.ref].buy or {}):
                cur = eco.items[a.ref].buy[cid]
                fixes.append(f"raise items.{a.ref}.buy to ≥ {eco.fmt_amount(cid, cur + step)} (now {eco.fmt_amount(cid, cur)})")
            elif a.kind == "sell" and cid in (eco.items[a.ref].sell or {}):
                cur = eco.items[a.ref].sell[cid]
                new = cur - step
                if new >= 0:
                    fixes.append(f"lower items.{a.ref}.sell to ≤ {eco.fmt_amount(cid, new)} (now {eco.fmt_amount(cid, cur)})")
                else:
                    fixes.append(f"remove items.{a.ref}.sell (making it unsellable)")
            elif a.kind == "craft":
                r = eco.recipes[a.ref]
                cur = r.cost.get(cid, 0)
                # each execution pays the cost once; expected-value loops count executions
                fixes.append(f"raise recipes.{a.ref}.cost to ≥ {eco.fmt_amount(cid, cur + step)} (now {eco.fmt_amount(cid, cur)})")
    if kind in ("duplication",):
        for k, i in acts:
            a = mk.actions[i]
            if a.kind == "craft":
                fixes.append(f"check recipes.{a.ref} quantities: inputs vs outputs")
    if kind == "free-item":
        for k, i in acts:
            fixes.append(f"give items.{mk.actions[i].ref} a non-zero buy price")
    return Pump(combo, net_i, kind, capital, lines, fixes)


# ----------------------------------------------------------------------------------------
# fastest possible progression

def loot_caps(eco: Economy) -> list[tuple[str, dict[str, int]]]:
    """Everything a single faucet event can hand out at once: one kill of a placed enemy (every
    drop roll succeeding at max quantity) or one respawn (item grants plus the largest currency
    increase the respawn formulas and currency grants can produce)."""
    from .combat import max_drop
    from .respawn import max_respawn_currency_gain
    caps: list[tuple[str, dict[str, int]]] = []
    for eid in sorted({v.enemy for l in eco.locations.values() if not l.safe for v in l.variants}):
        md = max_drop(eco, eid)
        if md:
            caps.append((f"{eid} (everything at once)", md))
    grant: dict[str, int] = {}
    for gr in eco.respawn.grants:
        if gr.kind == "item":
            grant[gr.id] = grant.get(gr.id, 0) + gr.qty
    for cid in eco.currencies:
        g = max_respawn_currency_gain(eco, cid)
        if g > 0:
            grant[cid] = g
    if grant:
        caps.append(("respawn (grants and currency formulas)", grant))
    return caps


def start_holdings(eco: Economy) -> dict[str, int]:
    h = {c: v for c, v in eco.start.currencies.items() if v}
    for i, q in eco.start.items.items():
        if q:
            h[i] = h.get(i, 0) + q
    return h


def _exactify(mk: Market, x: list[float], caps_le: list[dict[str, int]] | None = None,
              fixed: dict[int, Fraction] | None = None) -> list[Fraction] | None:
    """Exact rational point near the LP solution `x` with p ≥ 0, every action value ≤ 0,
    every cap value ≤ 1 and the `fixed` coordinates pinned."""
    caps_le = caps_le or []
    fixed = fixed or {}

    def ok(p: list[Fraction]) -> bool:
        if any(v < 0 for v in p) or any(p[i] != v for i, v in fixed.items()):
            return False
        for a in mk.actions:
            if sum(v * p[mk.index[g]] for g, v in a.delta.items()) > 0:
                return False
        return all(sum(q * p[mk.index[g]] for g, q in c.items()) <= 1 for c in caps_le)

    for limit in (10**3, 10**6, 10**9):
        p = [_rationalize(v, limit) if v > 1e-12 else Fraction(0) for v in x]
        for i, v in fixed.items():
            p[i] = v
        if ok(p):
            return p
    tol = 1e-7
    eqs = []
    for a in mk.actions:
        if abs(sum(float(v) * x[mk.index[g]] for g, v in a.delta.items())) <= tol:
            eqs.append(({mk.index[g]: v for g, v in a.delta.items()}, Fraction(0)))
    for c in caps_le:
        if abs(sum(q * x[mk.index[g]] for g, q in c.items()) - 1) <= tol:
            eqs.append(({mk.index[g]: Fraction(q) for g, q in c.items()}, Fraction(1)))
    for i, v in enumerate(x):
        if i not in fixed and v <= tol:
            eqs.append(({i: Fraction(1)}, Fraction(0)))
    for i, v in fixed.items():
        eqs.append(({i: Fraction(1)}, v))
    p = _solve_exact(eqs, len(x), [_rationalize(v, 10**6) for v in x])
    if p is not None and ok(p):
        return p
    return None


@dataclass
class IncomeCeiling:
    market: Market               # the lucky market (chancy recipes always succeed)
    prices: list[Fraction]       # valuation with the primary currency's base unit worth exactly 1
    D: Fraction                  # max shadow value of one faucet event, in primary base units
    caps: list[tuple[str, dict[str, int]]]


def income_ceiling(eco: Economy) -> IncomeCeiling | None:
    """Tightest provable cap on how much net worth (primary currency = 1) one kill or respawn can
    add, whatever players trade, craft or roll: min D s.t. no lucky-market action gains value and
    every loot cap is worth ≤ D. None if a lucky player can create currency without limit."""
    mk = build_market(eco, lucky=True)
    caps = loot_caps(eco)
    if not caps:
        return None
    n = len(mk.goods)
    A = mk.matrix()
    C = np.zeros((len(caps), n))
    for k, (_, c) in enumerate(caps):
        for g, q in c.items():
            C[k, mk.index[g]] += q
    rows = [np.concatenate([A[i], [0.0]]) for i in range(len(A))]
    rows += [np.concatenate([C[k], [-1.0]]) for k in range(len(caps))]
    A_eq = np.zeros((1, n + 1))
    ip = mk.index[eco.primary]
    A_eq[0, ip] = 1.0
    obj = np.zeros(n + 1)
    obj[n] = 1.0
    res = linprog(obj, A_ub=np.array(rows), b_ub=np.zeros(len(rows)), A_eq=A_eq, b_eq=[1.0],
                  bounds=[(0, None)] * (n + 1), method="highs")
    if res.status != 0:
        return None
    p = _exactify(mk, list(res.x[:n]), fixed={ip: Fraction(1)})
    if p is None:
        return None
    D = max(sum(q * p[mk.index[g]] for g, q in c.items()) for _, c in caps)
    return IncomeCeiling(mk, p, Fraction(D), caps)


@dataclass
class GrindBound:
    item: str
    events: int                  # N: at least this many faucet events (≈ encounters) before owning one
    prices: list[Fraction]       # the valuation proving it
    D: Fraction                  # max shadow value of one faucet event
    start_worth: Fraction
    item_price: Fraction


def grind_bounds(eco: Economy, items: list[str] | None = None) -> dict[str, GrindBound]:
    """For each item: the fewest faucet events (kills/respawns) any strategy needs to own one,
    starting from the player's start state, however lucky (chancy recipes always succeed and every
    drop roll comes up at max quantity). Solves max (p_x − W0·p) s.t. no action gains value and
    every loot cap is worth ≤ 1 (Charnes–Cooper form of max (p_x − W0·p)/D)."""
    mk = build_market(eco, lucky=True)
    caps = [c for _, c in loot_caps(eco)]
    if not caps:
        return {}
    n = len(mk.goods)
    A = mk.matrix()
    C = np.zeros((len(caps), n))
    for k, c in enumerate(caps):
        for g, q in c.items():
            C[k, mk.index[g]] += q
    start = start_holdings(eco)
    w0 = np.zeros(n)
    for g, q in start.items():
        w0[mk.index[g]] += q
    A_ub = np.vstack([A, C]) if len(A) else C
    b_ub = np.concatenate([np.zeros(len(A)), np.ones(len(caps))])
    out: dict[str, GrindBound] = {}
    for item in items or [i for i in eco.items]:
        ix = mk.index[item]
        c = w0.copy()
        c[ix] -= 1.0
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=[(0, None)] * n, method="highs")
        if res.status != 0 or -res.fun <= 1e-9:
            continue  # unbounded (unobtainable item) or no useful bound
        p = _exactify(mk, list(res.x), caps_le=caps)
        if p is None:
            continue
        D = max(sum(q * p[mk.index[g]] for g, q in cp.items()) for cp in caps)
        W = sum(q * p[mk.index[g]] for g, q in start.items())
        px = p[ix]
        if D <= 0 or px - W <= 0:
            continue
        N = math.ceil((px - W) / D)
        if N >= 1:
            out[item] = GrindBound(item, N, p, Fraction(D), Fraction(W), px)
    return out
