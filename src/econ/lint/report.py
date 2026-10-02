"""Text and JSON rendering of lint reports."""

from __future__ import annotations

import json
import math

from .runner import LintReport


def _clean(x):
    if isinstance(x, float):
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        if math.isnan(x):
            return None
        return round(x, 6)
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def to_json(rep: LintReport) -> str:
    out = {
        "source": rep.source,
        "economy": rep.economy.name if rep.economy else None,
        "summary": {"errors": rep.count("error"), "warnings": rep.count("warning"), "info": rep.count("info"),
                    "ignored": rep.ignored, "seconds": round(rep.seconds, 2)},
        "findings": [f.to_dict() for f in rep.findings],
        "metrics": rep.metrics,
    }
    return json.dumps(_clean(out), indent=2, ensure_ascii=False)


def _life(x) -> str:
    if x is None:
        return "?"
    if isinstance(x, str) or math.isinf(x) or x > 1e12:
        return "∞"
    return f"{x:,.0f}" if x >= 10 else f"{x:.1f}"


def to_text(rep: LintReport, details: bool = False, metrics: bool = True) -> str:
    L: list[str] = []
    name = rep.economy.name if rep.economy else "?"
    e, w, i = rep.count("error"), rep.count("warning"), rep.count("info")
    lean = rep.metrics.get("lean")
    lean_s = ""
    if lean:
        n_ok = sum(1 for v in lean["theorems"].values() if v == "proved")
        lean_s = f" · Lean {'✓' if lean['ok'] else '✗'} {n_ok}/{len(lean['theorems'])} theorems"
    head = "✗" if e else ("!" if w else "✓")
    L.append(f"econ lint · {name} ({rep.source})")
    L.append(f"{head} {e} error{'s' * (e != 1)} · {w} warning{'s' * (w != 1)} · {i} info{lean_s}"
             + (f" · {rep.ignored} ignored" if rep.ignored else "") + f" · {rep.seconds:.1f}s")
    for f in rep.findings:
        L.append("")
        loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "")
        L.append(f"{f.severity.upper()} [{f.code}]" + (f" {f.path}" if f.path else "") + (f"  ({loc})" if loc else ""))
        L.append(f"  {f.message}")
        show = details or f.severity != "info" or f.code == "lean"
        if show and f.detail:
            for line in f.detail.splitlines():
                L.append(f"    {line}")
        if show and f.fix:
            L.append(f"  fix: {f.fix}")
        if f.lean:
            L.append(f"  lean: {f.lean}")
    if metrics and rep.economy is not None and rep.metrics:
        L.append("")
        L.extend(_metrics_text(rep))
    if rep.findings and any(f.severity in ("error", "warning") for f in rep.findings):
        L.append("")
        L.append("Explain a rule: `econ explain <code>` · machine-readable output: `--format json`")
    return "\n".join(L)


def _metrics_text(rep: LintReport) -> list[str]:
    eco = rep.economy
    m = rep.metrics
    cur = eco.currencies[eco.primary]
    L = ["── Metrics " + "─" * 60]
    mk = m.get("market")
    if mk:
        L.append(f"Market: {mk['status']} ({mk['goods']} goods, {mk['actions']} actions)")
        if mk.get("income_ceiling_per_event") is not None:
            L.append(f"  income ceiling: no strategy or luck gains more than "
                     f"{cur.format(mk['income_ceiling_per_event'])} of net worth per kill/respawn")
        if mk.get("shadow_prices"):
            items = [(g, v) for g, v in mk["shadow_prices"].items() if g in eco.items and v]
            items.sort(key=lambda t: -t[1])
            L.append("  shadow prices (consistent valuation, in " + eco.primary + "): "
                     + ", ".join(f"{g} {cur.format(v)}" for g, v in items[:10]) + (" …" if len(items) > 10 else ""))
    if "even_fight_win" in m:
        ef = m["even_fight_win"]
        L.append(f"Battle: even fight win {ef['min']:.1%}" + (f"–{ef['max']:.1%}" if ef['max'] - ef['min'] > 5e-4 else "")
                 + " · odds by level difference: "
                 + ", ".join(f"{k.split()[1]}: {v:.0%}" for k, v in m.get("odds_table", {}).items()))
    locs = m.get("locations")
    if locs:
        L.append("Difficulty map — lifespan = expected encounters per life; geared = best obtainable gear for the level")
        L.append(f"  {'location':<12} {'lvl':>3}  {'death%':>7}  {'life naked/geared':>18}  {'xp/enc':>7}  {'enc/lvl':>7}  {'income/enc':>10}")
        pace = {p["level"]: p for p in m.get("leveling", {}).get("per_level", [])}
        upkeep = {u["level"]: u for u in m.get("upkeep", [])}
        for t in locs:
            for k, r in enumerate(t["rows"]):
                lvl = r["level"]
                need = eco.xp_to_next(lvl)
                epl = (need / r["xp_per_encounter"]) if r["xp_per_encounter"] > 0 and need else None
                L.append(f"  {t['location'] if k == 0 else '':<12} {lvl:>3}  {r['death_prob_geared']:>7.2%}  "
                         f"{_life(r['lifespan_naked']):>8} / {_life(r['lifespan_geared']):<8}  "
                         f"{r['xp_per_encounter']:>7.1f}  {('%.1f' % epl) if epl else '–':>7}  "
                         f"{cur.format(r['income_per_encounter']):>10}")
        spawn = []
        for t in locs:
            s = t["spawn"]
            g, n = s.get("geared", {}), s.get("naked", {})
            if g.get("lifespan") is not None:
                spawn.append(f"{t['location']} L{t['level_range'][0]}: geared {_life(g['lifespan'])} → dies ~L{g['death_level']:.1f}"
                             f", naked {_life(n.get('lifespan'))}")
        if spawn:
            L.append("  spawn study (start of range, stay and level up): " + " · ".join(spawn))
    lv = m.get("leveling")
    if lv and lv.get("uncovered_levels"):
        L.append(f"Levelling: total time unknown — no location covers level(s) "
                 f"{', '.join(map(str, lv['uncovered_levels'][:10]))}")
    elif lv and lv.get("hours_to_max") is not None and lv.get("encounters_to_max") is not None:
        L.append(f"Levelling: level 1 → {eco.max_level} takes ~{lv['encounters_to_max']:,.0f} encounters at the intended locations "
                 f"(≈ {lv['hours_to_max']:.1f} h of play)")
    fast = m.get("fastest")
    if fast:
        proved = any("lean" in v for v in fast.values())
        L.append("Fastest possible acquisition from player.start (any strategy, any luck; ≥ kills/respawns)"
                 + (" — proved in Lean" if proved else "") + ": "
                 + ", ".join(f"{k} ≥ {v['events']}" for k, v in list(fast.items())[:10]))
    up = m.get("upkeep")
    if up:
        worst = max(up, key=lambda u: u["upkeep"] / max(u["income"], 1e-9))
        L.append(f"Upkeep: worst at level {worst['level']}: {cur.format(worst['upkeep'])}/encounter vs income "
                 f"{cur.format(worst['income'])} ({worst['loadout']})")
    qs = m.get("quick_sim")
    if qs:
        lr = qs.get("levels_reached", {})
        last = max((int(k) for k in lr), default=None)
        L.append(f"Quick sim ({qs['runs']} runs × {qs['encounters']:,} enc, player.policy): median final level "
                 f"{qs['final_level_p50']:.0f}" + (f" (L{last} at enc {lr[str(last)]:,.0f})" if last else "")
                 + f", {qs['deaths_per_100_encounters']:.2f} deaths/100 enc, first life median {qs['first_life_p50']:.0f} enc, "
                 f"money {cur.format(qs['final_money_p50'])}; time: "
                 + ", ".join(f"{k} {v:.0%}" for k, v in list(qs["time_share_by_location"].items())[:4]))
    return L
