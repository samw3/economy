"""`econ diff A B`: what changed between two versions of an economy (findings and key metrics)."""

from __future__ import annotations

import math

from .runner import LintReport


def _key(f) -> tuple:
    return (f.severity, f.code, f.path)


def _num(x):
    if isinstance(x, str):
        return math.inf if x == "inf" else None
    return x


def _flat(rep: LintReport) -> dict[str, float]:
    """Key metrics flattened to 'name' -> number."""
    m = rep.metrics
    out: dict[str, float] = {}
    for t in m.get("locations", []):
        for r in t["rows"]:
            k = f"{t['location']} L{r['level']}"
            out[f"{k} lifespan geared"] = _num(r["lifespan_geared"])
            out[f"{k} lifespan naked"] = _num(r["lifespan_naked"])
            out[f"{k} xp/enc"] = r["xp_per_encounter"]
            out[f"{k} income/enc"] = r["income_per_encounter"]
        for kind in ("geared", "naked"):
            v = t["spawn"].get(kind, {}).get("lifespan")
            if v is not None:
                out[f"{t['location']} spawn lifespan {kind}"] = _num(v)
    lv = m.get("leveling", {})
    if lv.get("hours_to_max") is not None:
        out["hours to max level"] = lv["hours_to_max"]
    for p in lv.get("per_level", []):
        if p.get("encounters") is not None:
            out[f"encounters for level {p['level']}→{p['level'] + 1}"] = _num(p["encounters"])
    ef = m.get("even_fight_win")
    if ef:
        out["even fight win"] = ef["min"]
    for item, v in m.get("fastest", {}).items():
        out[f"fastest {item} (events)"] = v["events"]
    qs = m.get("quick_sim")
    if qs:
        out["sim final level"] = qs["final_level_p50"]
        out["sim deaths/100 enc"] = qs["deaths_per_100_encounters"]
        out["sim first life"] = qs["first_life_p50"]
        out["sim final money"] = qs["final_money_p50"]
    for cid, w in m.get("wallet", {}).items():
        out[f"{cid} wallet cap"] = w["cap"]
        if w.get("fill_encounters") is not None:
            out[f"{cid} wallet fill (encounters)"] = w["fill_encounters"]
    for g, v in m.get("market", {}).get("shadow_prices", {}).items():
        out[f"shadow price {g}"] = v
    return out


def diff_text(a: LintReport, b: LintReport, threshold: float = 0.02, limit: int = 60) -> str:
    L: list[str] = []
    L.append(f"econ diff · {a.source} → {b.source}")
    L.append(f"  errors {a.count('error')} → {b.count('error')} · warnings {a.count('warning')} → {b.count('warning')}"
             f" · info {a.count('info')} → {b.count('info')}")
    ma, mb = a.metrics.get("market", {}).get("status"), b.metrics.get("market", {}).get("status")
    if ma != mb:
        L.append(f"  market: {ma} → {mb}")
    fa = {_key(f): f for f in a.findings if f.code != "lean"}
    fb = {_key(f): f for f in b.findings if f.code != "lean"}
    fixed = [fa[k] for k in fa if k not in fb]
    new = [fb[k] for k in fb if k not in fa]
    changed = [(fa[k], fb[k]) for k in fa if k in fb and fa[k].message != fb[k].message]
    if fixed:
        L.append("")
        L.append(f"Resolved ({len(fixed)}):")
        for f in fixed:
            L.append(f"  − {f.severity.upper()} [{f.code}] {f.path}: {f.message}")
    if new:
        L.append("")
        L.append(f"New ({len(new)}):")
        for f in new:
            L.append(f"  + {f.severity.upper()} [{f.code}] {f.path}: {f.message}")
    if changed:
        L.append("")
        L.append(f"Changed ({len(changed)}):")
        for x, y in changed:
            L.append(f"  ~ [{y.code}] {y.path}: {x.message}")
            L.append(f"    → {y.message}")
    xa, xb = _flat(a), _flat(b)
    rows = []
    added = sorted(k for k in xb if k not in xa)
    removed = sorted(k for k in xa if k not in xb)
    for k in xa.keys() & xb.keys():
        va, vb = xa[k], xb[k]
        if va is None or vb is None or va == vb:
            continue
        if math.isinf(va) or math.isinf(vb):
            rows.append((math.inf, k, va, vb))
            continue
        rel = abs(vb - va) / max(abs(va), 1e-9)
        if rel >= threshold:
            rows.append((rel, k, va, vb))
    if rows:
        rows.sort(key=lambda r: (-r[0], r[1]))
        L.append("")
        L.append(f"Metrics that moved ≥ {threshold:.0%} ({len(rows)}; largest first):")
        for rel, k, va, vb in rows[:limit]:
            arrow = "↑" if vb > va else "↓"
            pct = "" if (math.isinf(rel) or not va) else f" ({(vb - va) / abs(va):+.0%})"
            L.append(f"  {k:<40} {_f(va):>10} → {_f(vb):<10} {arrow}{pct}")
        if len(rows) > limit:
            L.append(f"  … {len(rows) - limit} more (raise --limit)")
    for label, keys in (("New metrics", added), ("Removed metrics", removed)):
        if keys:
            groups: dict[str, int] = {}
            for k in keys:
                head = k.split(" ")[0]
                groups[head] = groups.get(head, 0) + 1
            L.append("")
            L.append(f"{label} ({len(keys)}): " + ", ".join(f"{g} ×{n}" if n > 1 else g for g, n in groups.items()))
    if not (fixed or new or changed or rows or added or removed):
        L.append("No differences in findings or key metrics.")
    return "\n".join(L)


def _f(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    return f"{v:.3g}"
