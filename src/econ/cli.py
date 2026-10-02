"""Command line interface: `econ lint | sim | prove | tables | explain | schema | new`."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import sys
from pathlib import Path


def _load_or_die(path: str):
    from .economy import load_economy
    from .lint.report import to_text
    from .lint.runner import LintReport

    eco, findings, _ = load_economy(path)
    if eco is None:
        rep = LintReport(path, None, findings)
        print(to_text(rep, metrics=False), file=sys.stderr)
        sys.exit(2)
    return eco


# ----------------------------------------------------------------------------------------
def cmd_lint(a) -> int:
    from .lint.report import to_json, to_text
    from .lint.runner import lint

    rep = lint(a.path, lean=not a.no_lean, sim=not a.no_sim, sim_n=a.sim_n, sim_runs=a.sim_runs,
               use_cache=not a.no_cache)
    if a.format == "json":
        print(to_json(rep))
    else:
        print(to_text(rep, details=a.details, metrics=not a.no_metrics))
    if rep.count("error"):
        return 1
    if a.strict and rep.count("warning"):
        return 1
    return 0


# ----------------------------------------------------------------------------------------
def _parse_policy(eco, overrides: list[str]):
    pol = dataclasses.replace(eco.policy)
    for o in overrides or []:
        if "=" not in o:
            raise SystemExit(f"--policy expects key=value, got {o!r}")
        k, v = o.split("=", 1)
        k = k.strip().replace("-", "_")
        if not hasattr(pol, k):
            fields = ", ".join(f.name for f in dataclasses.fields(pol))
            raise SystemExit(f"unknown policy field {k!r} (fields: {fields})")
        cur = getattr(pol, k)
        if k == "reserve":
            val = eco.money.parse(v)
        elif isinstance(cur, bool):
            val = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(v)
        elif isinstance(cur, float):
            val = float(v)
        else:
            val = v
        if k == "location" and val != "auto" and val not in eco.locations:
            raise SystemExit(f"unknown location {val!r}")
        setattr(pol, k, val)
    return pol


def cmd_sim(a) -> int:
    from .sim.report import simulate

    eco = _load_or_die(a.path)
    policy = _parse_policy(eco, a.policy)
    start: dict = {}
    if a.level:
        if not 1 <= a.level <= eco.max_level:
            raise SystemExit(f"--level must be in 1..{eco.max_level}")
        start["level"] = a.level
    if a.location:
        if a.location not in eco.locations:
            raise SystemExit(f"unknown location {a.location!r}")
        start["location"] = a.location
        if not a.policy or not any(p.startswith("location=") for p in a.policy):
            policy.location = a.location
    if a.money is not None:
        money = a.money
        if money.strip().startswith("{"):
            from ruamel.yaml import YAML
            money = dict(YAML(typ="safe").load(money))
        start["money"] = eco.money.parse(money)
    if a.items is not None:
        items = {}
        for part in filter(None, (x.strip() for x in a.items.split(","))):
            k, _, v = part.partition("=")
            if k not in eco.items:
                raise SystemExit(f"unknown item {k!r}")
            items[k] = int(v or 1)
        start["items"] = items
    trace_rows = []
    trace = trace_rows.append if a.trace else None
    from .formula import FormulaError
    try:
        res = simulate(eco, a.n, runs=a.runs, seed=a.seed, every=a.every, policy=policy, start=start, trace=trace,
                       log=a.log is not None)
    except FormulaError as e:
        print(f"simulation stopped: a formula failed: {e}", file=sys.stderr)
        return 2
    if a.trace:
        p = Path(a.trace)
        if p.suffix == ".jsonl":
            with open(p, "w") as fh:
                for r in trace_rows:
                    fh.write(json.dumps(r) + "\n")
        else:
            with open(p, "w", newline="") as fh:
                if trace_rows:
                    w = csv.DictWriter(fh, fieldnames=list(trace_rows[0]))
                    w.writeheader()
                    w.writerows(trace_rows)
        print(f"wrote {len(trace_rows):,} encounter rows to {p}", file=sys.stderr)
    if a.format == "json":
        from .lint.report import _clean
        out = res.summary()
        if a.log is not None and res.stats:
            out["log"] = res.stats[0].log[: a.log or 200]
        print(json.dumps(_clean(out), indent=2))
    else:
        print(res.text(log_lines=(a.log or 60) if a.log is not None else 0))
    return 0


# ----------------------------------------------------------------------------------------
def cmd_diff(a) -> int:
    from .lint.diff import diff_text
    from .lint.runner import lint

    ra = lint(a.old, lean=False, sim=not a.no_sim)
    rb = lint(a.new, lean=False, sim=not a.no_sim)
    if ra.economy is None or rb.economy is None:
        bad = a.old if ra.economy is None else a.new
        print(f"{bad} does not compile; run `econ lint {bad}`", file=sys.stderr)
        return 2
    print(diff_text(ra, rb, threshold=a.threshold, limit=a.limit))
    return 0


# ----------------------------------------------------------------------------------------
def cmd_prove(a) -> int:
    from .lint.rules import Ctx, check_market
    from .proofs.generate import generate, module_name
    from .proofs.runner import check

    eco = _load_or_die(a.path)
    ctx = Ctx(eco)
    check_market(ctx)
    module = generate(eco, ctx, module_name(a.path, eco), source=a.path)
    res = check(module, use_cache=not a.no_cache)
    if a.show:
        print(module.text)
    if not res.available:
        print(f"Lean unavailable: {res.message}")
        return 2
    print(f"Lean {'✓' if res.ok else '✗'} {sum(v == 'proved' for v in res.statuses.values())}/{len(res.statuses)} theorems "
          f"in {res.seconds:.1f}s{' (cached)' if res.cached else ''} — {res.path}")
    for t in module.theorems:
        s = res.statuses.get(t.name, "?")
        mark = "✓" if s == "proved" else "✗"
        print(f"  {mark} {t.name}: {t.statement}" + ("" if s == "proved" else f"\n      {s}"))
    if res.axioms:
        ax = sorted({x for v in res.axioms.values() for x in v})
        print(f"  axioms used: {', '.join(ax) if ax else 'none'}")
    for n in module.notes:
        print(f"  note: {n}")
    for e in res.errors:
        print(f"  error: {e}")
    return 0 if res.ok else 1


# ----------------------------------------------------------------------------------------
def cmd_tables(a) -> int:
    from .analysis.combat import drop_value, encounter_stats, loadout, obtainable_items, spawn_study, upkeep_per_encounter
    from .analysis.market import analyze_market

    eco = _load_or_die(a.path)
    only = set(a.only.split(",")) if a.only else {"odds", "lifespan", "xp", "drops", "prices"}
    pool = obtainable_items(eco)
    cur = eco.currencies[eco.primary]
    out: list[str] = []
    if "odds" in only:
        lv = sorted({v.level for l in eco.locations.values() for v in l.variants})
        out.append(f"Battle odds P(win): rows = player effective level, columns = enemy level")
        out.append("      " + "".join(f"{e:>7}" for e in lv))
        for p in range(1, eco.max_level + 1 + int(a.extra)):
            out.append(f"{p:>5} " + "".join(f"{eco.odds(p, e):>7.1%}" for e in lv))
        out.append("")
    if "lifespan" in only or "xp" in only:
        out.append("Per location and level (geared = best obtainable gear allowed at that level)")
        out.append(f"  {'location':<12} {'lvl':>3} {'gear power':>10} {'death% geared':>13} {'life naked':>10} "
                   f"{'life geared':>11} {'xp/enc':>7} {'enc/level':>9} {'income/enc':>10} {'upkeep/enc':>10}")
        for loc in eco.location_order():
            lo_, hi_ = loc.level_range or (1, eco.max_level)
            rng = range(1, eco.max_level + 1) if a.all_levels else range(lo_, min(hi_, eco.max_level) + 1)
            for k, lvl in enumerate(rng):
                g = loadout(eco, lvl, "geared", pool)
                sn = encounter_stats(eco, loc, lvl, 0.0)
                sg = encounter_stats(eco, loc, lvl, g.power)
                need = eco.xp_to_next(lvl)
                epl = f"{need / sg.xp:.1f}" if sg.xp > 0 and need else "–"
                out.append(f"  {loc.id if k == 0 else '':<12} {lvl:>3} {g.power:>10g} {sg.death_prob:>13.2%} "
                           f"{_life(sn.lifespan):>10} {_life(sg.lifespan):>11} {sg.xp:>7.1f} {epl:>9} "
                           f"{cur.format(sg.income.get(eco.primary, 0)):>10} {cur.format(upkeep_per_encounter(eco, g)):>10}")
            if loc.level_range:
                s = spawn_study(eco, loc, loc.level_range[0], "geared", pool)
                out.append(f"  {'':<12} spawn study from level {loc.level_range[0]} (geared, levelling): "
                           f"{_life(s.lifespan)} encounters, dies at level ~{s.death_level:.1f} {s.note}")
        out.append("")
    if "drops" in only:
        out.append("Enemies: expected liquid value per kill (currency + items at sell price)")
        for e in eco.enemies.values():
            dv = drop_value(eco, e.id)
            out.append(f"  {e.id:<14} L{e.level:<3} xp {e.xp:<6g} " + ", ".join(
                eco.currencies[c].format(v) for c, v in dv.items()))
        out.append("")
    if "prices" in only:
        res = analyze_market(eco)
        if res.prices is not None:
            out.append(f"Shadow prices (a consistent valuation certifying no money pump), in {eco.primary}:")
            for g in res.market.goods:
                sp = res.shadow_price(g, eco)
                it = eco.items.get(g)
                extra = ""
                if it:
                    extra = f"  (buy {eco.fmt(it.buy, empty='–')}, sell {eco.fmt(it.sell, empty='–')})"
                out.append(f"  {g:<18} {cur.format(sp) if sp is not None else '?':>12}{extra}")
        else:
            out.append(f"Market has {len(res.pumps)} pump(s); run `econ lint` for details.")
        out.append("")
    print("\n".join(out).rstrip())
    return 0


def _life(x: float) -> str:
    if math.isinf(x):
        return "∞"
    return f"{x:,.0f}" if x >= 10 else f"{x:.1f}"


# ----------------------------------------------------------------------------------------
def cmd_explain(a) -> int:
    from .formula import FUNCTION_HELP
    from .lint.findings import RULES

    topic = a.code
    if topic in (None, "rules", "list"):
        by_cat: dict[str, list] = {}
        for r in RULES.values():
            by_cat.setdefault(r.category, []).append(r)
        for cat, rules in by_cat.items():
            print(f"{cat}:")
            for r in rules:
                print(f"  {r.code:<24} {r.severity:<8} {r.summary}")
        print("\nAlso: `econ explain formulas`, `econ explain format`")
        return 0
    if topic == "formulas":
        print(FORMULA_HELP)
        print("Functions:")
        for k, v in FUNCTION_HELP.items():
            print(f"  {k:<55} {v}")
        return 0
    if topic == "format":
        doc = Path(__file__).resolve().parents[2] / "docs" / "FORMAT.md"
        print(doc.read_text() if doc.exists() else "See docs/FORMAT.md")
        return 0
    r = RULES.get(topic)
    if r is None:
        import difflib
        close = difflib.get_close_matches(topic, list(RULES), n=3)
        print(f"unknown rule {topic!r}" + (f"; did you mean {', '.join(close)}?" if close else ""))
        return 1
    print(f"{r.code} ({r.severity}, {r.category}): {r.summary}\n\n{r.explanation}\n\nHow to fix: {r.fix}")
    return 0


FORMULA_HELP = """Formulas are arithmetic expressions, e.g. `clamp(sigmoid((delta + bias) / spread), 0.01, 0.99)`.
Operators: + - * / // % ** (or ^), comparisons, and/or/not, `a if cond else b`. Numbers are floats.
Constants from the top-level `params:` section (and `battle.params`) can be used by name.

Variables by context:
  battle.formula             playerLevel (effective: level + gear + consumable), enemyLevel, delta = playerLevel - enemyLevel
  progression.xp_to_next     level                       (XP to go from level to level+1)
  progression.xp_gain        xp (enemy base XP), playerLevel, enemyLevel, delta
  respawn.level              level, xp, deaths
  respawn.xp                 level, xp, deaths, xpToNext
  respawn.currencies.<id>    amount, cap, level, deaths
"""


# ----------------------------------------------------------------------------------------
def cmd_schema(a) -> int:
    from .model import json_schema
    print(json.dumps(json_schema(), indent=2))
    return 0


def cmd_new(a) -> int:
    src = Path(__file__).resolve().parent / "templates" / "template.yaml"
    dst = Path(a.path)
    if dst.exists() and not a.force:
        print(f"{dst} exists (use --force to overwrite)", file=sys.stderr)
        return 1
    dst.write_text(src.read_text())
    print(f"wrote {dst} — now run: econ lint {dst}")
    return 0


# ----------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="econ", description="Game economy balancing harness.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lint", help="analyse an economy file: exploits, lifespans, pacing (+ Lean proofs)")
    p.add_argument("path", help="economy .yaml file or a directory of .yaml files")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--no-lean", action="store_true", help="skip Lean verification")
    p.add_argument("--no-sim", action="store_true", help="skip the quick simulation")
    p.add_argument("--sim-n", type=int, default=1500, help="encounters per quick-sim run (default 1500)")
    p.add_argument("--sim-runs", type=int, default=30, help="quick-sim runs (default 30)")
    p.add_argument("--details", action="store_true", help="show details for info findings too")
    p.add_argument("--no-metrics", action="store_true", help="omit the metrics section")
    p.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    p.add_argument("--no-cache", action="store_true", help="re-run Lean even if cached")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("sim", help="simulate n encounters for a player and show how they evolve")
    p.add_argument("path")
    p.add_argument("-n", type=int, default=1000, help="encounters per run (default 1000)")
    p.add_argument("--runs", type=int, default=50, help="independent runs (default 50)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--every", type=int, default=None, help="snapshot interval (default n/10)")
    p.add_argument("--level", type=int, help="override start level")
    p.add_argument("--location", help="start (and stay) at this location")
    p.add_argument("--money", help="override start money, e.g. '2s 50c' or '{coin: 500, gem: 3}'")
    p.add_argument("--items", help="override start items, e.g. 'iron_sword=1,health_potion=3'")
    p.add_argument("--policy", action="append", metavar="KEY=VALUE",
                   help="override player.policy, e.g. --policy craft=none --policy max_death_risk=0.05")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--trace", help="write every encounter of every run to a .csv or .jsonl file")
    p.add_argument("--log", type=int, nargs="?", const=60, help="print the event log of run 0 (optionally N lines)")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("diff", help="compare findings and key metrics of two versions of an economy")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--threshold", type=float, default=0.02, help="min relative change to show (default 0.02)")
    p.add_argument("--limit", type=int, default=60, help="max metric rows")
    p.add_argument("--no-sim", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("prove", help="generate the Lean model of the economy and check it")
    p.add_argument("path")
    p.add_argument("--show", action="store_true", help="print the generated Lean file")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_prove)

    p = sub.add_parser("tables", help="print analytic tables (odds, lifespan, xp, drops, prices)")
    p.add_argument("path")
    p.add_argument("--only", help="comma list of: odds,lifespan,xp,drops,prices")
    p.add_argument("--all-levels", action="store_true", help="all player levels for every location")
    p.add_argument("--extra", type=int, default=0, help="extra player levels beyond max in the odds table")
    p.set_defaults(fn=cmd_tables)

    p = sub.add_parser("explain", help="explain a lint rule (or 'formulas', 'format', 'rules')")
    p.add_argument("code", nargs="?")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("schema", help="print the JSON schema of the economy format")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("new", help="write a commented template economy file")
    p.add_argument("path")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_new)

    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
