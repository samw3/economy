"""`econ lint`: load, analyse, (optionally) simulate and verify in Lean, then report."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field

from ..economy import Economy, load_economy
from .findings import SEVERITY_ORDER, Finding
from .rules import ALL_CHECKS, Ctx, check_sim


@dataclass
class LintReport:
    source: str
    economy: Economy | None
    findings: list[Finding]
    metrics: dict = field(default_factory=dict)
    lean: object | None = None          # proofs.runner.LeanResult
    lean_module: object | None = None   # proofs.generate.LeanModule
    ignored: int = 0
    seconds: float = 0.0

    def count(self, sev: str) -> int:
        return sum(1 for f in self.findings if f.severity == sev)

    @property
    def ok(self) -> bool:
        return self.count("error") == 0


def lint(path: str, *, lean: bool = True, sim: bool = True, sim_n: int = 1500, sim_runs: int = 30,
         seed: int = 1, use_cache: bool = True) -> LintReport:
    t0 = time.time()
    eco, findings, res = load_economy(path)
    if eco is None:
        findings.sort(key=_key)
        return LintReport(path, None, findings, seconds=time.time() - t0)
    ctx = Ctx(eco)
    ctx.findings.extend(findings)
    for check in ALL_CHECKS:
        _guard(ctx, check.__name__, lambda: check(ctx))
    if sim:
        _guard(ctx, "check_sim", lambda: check_sim(ctx, sim_n, sim_runs, seed))
    report = LintReport(path, eco, ctx.findings, ctx.metrics)
    if lean:
        _guard(ctx, "lean", lambda: _run_lean(ctx, report, path, use_cache))
    # ignores
    kept: list[Finding] = []
    for f in ctx.findings:
        if any(r.code == f.code and (r.path is None or f.path.startswith(r.path)) for r in eco.ignore):
            report.ignored += 1
            continue
        kept.append(f)
    kept.sort(key=_key)
    report.findings = kept
    report.seconds = time.time() - t0
    return report


def _key(f: Finding):
    return (SEVERITY_ORDER[f.severity], f.code, f.path)


def _guard(ctx: Ctx, name: str, fn):
    try:
        fn()
    except Exception as e:  # never let one analysis crash the whole lint
        tb = traceback.format_exc(limit=4)
        ctx.findings.append(Finding("internal-error", "warning", f"{name} crashed: {type(e).__name__}: {e}",
                                    detail=tb))


def _run_lean(ctx: Ctx, report: LintReport, path: str, use_cache: bool):
    from ..proofs.generate import generate, module_name
    from ..proofs.runner import check

    eco = ctx.eco
    module = generate(eco, ctx, module_name(path, eco), source=path)
    report.lean_module = module
    res = check(module, use_cache=use_cache)
    report.lean = res
    if not res.available:
        ctx.add("lean-unavailable", "", f"Lean verification skipped: {res.message}")
        return
    st = res.statuses
    by_name = {t.name: t for t in module.theorems}

    def status(names: list[str]) -> str:
        bad = [n for n in names if st.get(n) != "proved"]
        if not names:
            return ""
        if bad:
            return f"NOT verified: {', '.join(bad)} ({st.get(bad[0])})"
        return f"verified by Lean kernel: {', '.join(names[:4])}{' …' if len(names) > 4 else ''}"

    for f in ctx.findings:
        if f.code in ("money-pump", "duplication", "free-item", "buy-below-sell") and "pump_index" in f.data:
            f.lean = status([f"pump{f.data['pump_index'] + 1}_is_pump"])
        elif f.code in ("lifespan-short", "lifespan-long") and "spawn_lifespan" in f.data:
            f.lean = "not modelled in Lean (spawn study: exact dynamic program in Python)"
        elif f.code in ("lifespan-short", "lifespan-long"):
            loc = f.path.split(".", 1)[1] if "." in f.path else ""
            kind, verdict = ("geared", "short") if f.code == "lifespan-short" else ("naked", "long")
            levels = set(f.data.get("levels", []))
            names = [t.name for t in module.theorems if t.kind == "lifespan" and t.meta.get("location") == loc
                     and t.meta.get("loadout") == kind and t.meta.get("verdict") == verdict
                     and (not levels or t.meta.get("level") in levels)]
            if names:
                f.lean = status(names)
            else:
                f.lean = "borderline: the rounded Lean model does not confirm this claim"
    proved = sum(1 for v in st.values() if v == "proved")
    total = len(st)
    parts = []
    if "no_money_pump" in st:
        parts.append("no money pump" + ("" if st["no_money_pump"] == "proved" else " (FAILED)"))
    if "income_ceiling" in st and st["income_ceiling"] == "proved":
        parts.append(f"income ceiling {eco.fmt_amount(eco.primary, by_name['income_ceiling'].meta['per_event'])} "
                     "per kill/respawn (any luck)")
    pumps = [n for n in st if n.endswith("_is_pump")]
    if pumps:
        parts.append(f"{sum(1 for n in pumps if st[n] == 'proved')}/{len(pumps)} exploit loops confirmed")
    lives = [t for t in module.theorems if t.kind == "lifespan"]
    if lives:
        parts.append(f"{sum(1 for t in lives if st.get(t.name) == 'proved')}/{len(lives)} lifespan claims")
    fast = [t for t in module.theorems if t.kind == "fastest"]
    if fast:
        parts.append(f"{sum(1 for t in fast if st.get(t.name) == 'proved')}/{len(fast)} fastest-progression bounds")
    for t in fast:
        if st.get(t.name) == "proved" and t.meta.get("item") in report.metrics.get("fastest", {}):
            report.metrics["fastest"][t.meta["item"]]["lean"] = t.name
    mono = [t for t in module.theorems if t.kind == "monotone"]
    if mono:
        parts.append(f"{sum(1 for t in mono if st.get(t.name) == 'proved')}/{len(mono)} monotonicity chains")
    ax = sorted({a for v in res.axioms.values() for a in v})
    standard = {"propext", "Classical.choice", "Quot.sound"}
    if not res.axioms:
        axs = "axioms: not reported"
    elif set(ax) <= standard:
        axs = f"axioms: only Lean's standard axioms ({', '.join(ax) or 'none'}) — no sorry, no native_decide"
    else:
        axs = "axioms: " + ", ".join(ax) + " (WARNING: non-standard axioms)"
    ctx.add("lean", "", f"Lean: {proved}/{total} theorems verified in {res.seconds:.1f}s"
            + (" (cached)" if res.cached else "") + (f" — {'; '.join(parts)}" if parts else ""),
            detail=f"file: {res.path}\n{axs}" + (f"\nnotes: {'; '.join(module.notes)}" if module.notes else ""),
            data={"file": res.path, "proved": proved, "total": total})
    for name, s in st.items():
        if s != "proved":
            t = by_name.get(name)
            ctx.add("lean-mismatch", "", f"Lean could not verify {name}: {s}",
                    detail=(t.statement if t else "") + f"\nsee {res.path}" + (f" lines {t.start}-{t.end}" if t else ""))
    for e in res.errors:
        if not any(e.startswith(f"line {t.start}") for t in module.theorems):
            pass
    report.metrics["lean"] = res.to_dict()
    report.metrics["lean"]["claims"] = [{"theorem": t.name, "kind": t.kind, "statement": t.statement,
                                         "status": st.get(t.name)} for t in module.theorems]
