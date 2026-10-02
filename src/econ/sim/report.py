"""Run many simulations and summarise how players evolve."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from ..economy import Economy, Policy
from .engine import RunStats, Simulator


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def fmt_time(encounters: float, rate: float) -> str:
    minutes = encounters / rate
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


@dataclass
class SimResult:
    eco: Economy
    n: int
    runs: int
    seed: int
    policy: Policy
    start_desc: str
    stats: list[RunStats] = field(default_factory=list)

    # ------------------------------------------------------------------------------------
    def summary(self) -> dict:
        eco = self.eco
        cid = eco.primary
        out: dict = {
            "economy": eco.name, "encounters": self.n, "runs": self.runs, "seed": self.seed,
            "encounter_rate_per_min": eco.encounter_rate, "start": self.start_desc,
            "policy": _policy_dict(self.policy),
        }
        # timeline
        timeline = []
        n_snap = min(len(s.snapshots) for s in self.stats)
        for i in range(n_snap):
            snaps = [s.snapshots[i] for s in self.stats]
            lv = [x.level for x in snaps]
            money = [x.money.get(cid, 0) for x in snaps]
            worth = [x.worth for x in snaps]
            locs = Counter(x.location for x in snaps)
            timeline.append({
                "encounter": snaps[0].encounter,
                "level": {"p10": pct(lv, .1), "p50": pct(lv, .5), "p90": pct(lv, .9)},
                "eff_level_p50": pct([x.eff_level for x in snaps], .5),
                "money": {"p10": pct(money, .1), "p50": pct(money, .5), "p90": pct(money, .9)},
                "worth_p50": pct(worth, .5),
                "deaths_p50": pct([x.deaths for x in snaps], .5),
                "deaths_mean": statistics.fmean(x.deaths for x in snaps),
                "location_mode": locs.most_common(1)[0][0],
                "location_share": round(locs.most_common(1)[0][1] / len(snaps), 3),
            })
        out["timeline"] = timeline
        # lives
        lives = [l for s in self.stats for l in s.lifespans]
        first = [s.lifespans[0] if s.lifespans else s.final_life for s in self.stats]
        first_censored = sum(1 for s in self.stats if not s.lifespans)
        deaths = [len(s.lifespans) for s in self.stats]
        out["lives"] = {
            "deaths_total": sum(deaths),
            "deaths_per_run_mean": statistics.fmean(deaths),
            "deaths_per_100_encounters": 100 * sum(deaths) / (self.runs * self.n),
            "completed_lives": len(lives),
            "lifespan_mean": statistics.fmean(lives) if lives else None,
            "lifespan_p10": pct(lives, .1) if lives else None,
            "lifespan_p50": pct(lives, .5) if lives else None,
            "lifespan_p90": pct(lives, .9) if lives else None,
            "first_life_p50": pct(first, .5),
            "first_life_never_died_share": first_censored / self.runs,
            "note": "lifespan = encounters from spawn until death, counting the fatal one",
        }
        da = Counter()
        db = Counter()
        for s in self.stats:
            da.update(s.deaths_at)
            db.update(s.deaths_by)
        tot = sum(da.values()) or 1
        out["deaths_by_location"] = {k: round(v / tot, 3) for k, v in da.most_common()}
        out["deaths_by_enemy"] = {k: round(v / tot, 3) for k, v in db.most_common()}
        # levels
        lv: dict[int, list[int]] = {}
        for s in self.stats:
            for level, enc in s.level_reached.items():
                lv.setdefault(level, []).append(enc)
        out["levels"] = {
            str(level): {"reached_share": round(len(v) / self.runs, 3), "encounter_p50": pct(v, .5)}
            for level, v in sorted(lv.items())
        }
        acq: dict[str, list[int]] = {}
        for st in self.stats:
            for iid, enc in st.acquired.items():
                acq.setdefault(iid, []).append(enc)
        out["first_acquired"] = {
            iid: {"share": round(len(v) / self.runs, 3), "encounter_p50": pct(v, .5)}
            for iid, v in sorted(acq.items(), key=lambda t: pct(t[1], .5))
        }
        # flows
        flows: dict[str, dict[str, float]] = {}
        for s in self.stats:
            for cat, d in s.flows.items():
                for c, v in d.items():
                    flows.setdefault(cat, {}).setdefault(c, 0.0)
                    flows[cat][c] += v / self.runs
        out["flows_per_run"] = flows
        items: dict[str, dict[str, float]] = {}
        for s in self.stats:
            for cat, d in s.items.items():
                for i, v in d.items():
                    items.setdefault(cat, {}).setdefault(i, 0.0)
                    items[cat][i] += v / self.runs
        out["items_per_run"] = items
        kills = Counter()
        time_at = Counter()
        for s in self.stats:
            kills.update(s.kills)
            time_at.update(s.time_at)
        out["kills_per_run"] = {k: round(v / self.runs, 2) for k, v in kills.most_common()}
        tt = sum(time_at.values()) or 1
        out["time_share_by_location"] = {k: round(v / tt, 3) for k, v in time_at.most_common()}
        finals = [s.final for s in self.stats if s.final]
        inv = Counter()
        for f in finals:
            inv.update({k: v for k, v in f.inv.items() if v > 0})
            inv.update(Counter(f.equipped.values()))
        out["final"] = {
            "level_p50": pct([f.level for f in finals], .5),
            "money_p50": pct([f.money.get(cid, 0) for f in finals], .5),
            "worth_p50": pct([s.snapshots[-1].worth for s in self.stats], .5),
            "equipped_mode": dict(Counter(tuple(sorted(f.equipped.items())) for f in finals).most_common(1)[0][0])
            if finals else {},
            "inventory_mean": {k: round(v / len(finals), 2) for k, v in inv.most_common(12)},
        }
        return out

    # ------------------------------------------------------------------------------------
    def text(self, log_lines: int = 0) -> str:
        eco = self.eco
        s = self.summary()
        rate = eco.encounter_rate
        cur = eco.currencies[eco.primary]
        L: list[str] = []
        L.append(f"Simulation · {eco.name} · {self.runs} runs × {self.n:,} encounters (seed {self.seed}) · "
                 f"≈ {fmt_time(self.n, rate)} of play each at {rate:g} encounters/min")
        L.append(f"Start: {self.start_desc}")
        L.append(f"Policy: {_policy_text(self.policy, eco)}")
        L.append("")
        L.append("Player evolution (median [p10–p90] across runs)")
        rows = []
        for row in s["timeline"]:
            lv = row["level"]
            lvs = f"{lv['p50']:.0f}" + (f" [{lv['p10']:.0f}–{lv['p90']:.0f}]" if lv["p10"] != lv["p90"] else "")
            m = row["money"]
            ms = cur.format(round(m["p50"]))
            if m["p10"] != m["p90"]:
                ms += f" [{cur.format(round(m['p10']))}–{cur.format(round(m['p90']))}]"
            where = row["location_mode"] + ("" if row["location_share"] > 0.75 else f" ({row['location_share']:.0%})")
            rows.append((f"{row['encounter']:,}", fmt_time(row['encounter'], rate), lvs, f"{row['eff_level_p50']:.1f}",
                         ms, cur.format(round(row['worth_p50'])), f"{row['deaths_mean']:.1f}", where))
        head = ("enc", "time", "level", "eff", "money", "worth", "deaths", "where")
        wid = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(len(head))]
        right = {0, 1, 3, 5, 6}
        def fmt_row(r):
            return "  " + "  ".join(r[i].rjust(wid[i]) if i in right else r[i].ljust(wid[i]) for i in range(len(r))).rstrip()
        L.append(fmt_row(head))
        L.extend(fmt_row(r) for r in rows)
        L.append("")
        lv = s["levels"]
        marks = []
        for level in sorted(int(k) for k in lv):
            if level == eco.start.level:
                continue
            d = lv[str(level)]
            if level % max(1, eco.max_level // 6) == 0 or level == eco.max_level or len(lv) <= 8:
                marks.append(f"L{level} @ {d['encounter_p50']:,.0f} enc ({d['reached_share']:.0%})")
        if marks:
            L.append("Levels reached (median encounter, share of runs): " + " · ".join(marks))
        gear = [(i, d) for i, d in s["first_acquired"].items() if i in eco.items and eco.items[i].slot and d["encounter_p50"] > 0]
        if gear:
            L.append("Gear first owned (median encounter, share of runs): "
                     + " · ".join(f"{i} @ {d['encounter_p50']:,.0f} ({d['share']:.0%})" for i, d in gear[:8]))
        lives = s["lives"]
        if lives["completed_lives"]:
            L.append(f"Lives: {lives['deaths_total']} deaths ({lives['deaths_per_run_mean']:.1f}/run, "
                     f"{lives['deaths_per_100_encounters']:.2f} per 100 encounters). Encounters per life: "
                     f"mean {lives['lifespan_mean']:.0f}, median {lives['lifespan_p50']:.0f}, "
                     f"p10 {lives['lifespan_p10']:.0f}, p90 {lives['lifespan_p90']:.0f}")
        else:
            L.append("Lives: no deaths in any run.")
        L.append(f"       first life: median {lives['first_life_p50']:.0f} encounters "
                 f"(≈ {fmt_time(lives['first_life_p50'], rate)}); "
                 f"{lives['first_life_never_died_share']:.0%} of runs never died")
        if s["deaths_by_location"]:
            L.append("Deaths by location: " + ", ".join(f"{k} {v:.0%}" for k, v in list(s["deaths_by_location"].items())[:5])
                     + " · by enemy: " + ", ".join(f"{k} {v:.0%}" for k, v in list(s["deaths_by_enemy"].items())[:5]))
        L.append("Time by location: " + ", ".join(f"{k} {v:.0%}" for k, v in list(s["time_share_by_location"].items())[:6]))
        L.append("")
        L.append(f"Money flows per run (mean):")
        flows = s["flows_per_run"]
        for cid in eco.currencies:
            c = eco.currencies[cid]
            plus = [(k, v[cid]) for k, v in flows.items() if v.get(cid, 0) > 0 and k != "overflow"]
            minus = [(k, -v[cid]) for k, v in flows.items() if v.get(cid, 0) < 0 and k != "overflow"]
            over = flows.get("overflow", {}).get(cid, 0)
            if not plus and not minus:
                continue
            net = sum(v for _, v in plus) - sum(v for _, v in minus)
            L.append(f"  {cid}: + " + (" · ".join(f"{k} {c.format(round(v))}" for k, v in plus) or "nothing"))
            L.append(f"  {' ' * len(cid)}  − " + (" · ".join(f"{k} {c.format(round(v))}" for k, v in minus) or "nothing")
                     + (f" · wasted by wallet cap {c.format(round(over))}" if over else ""))
            L.append(f"  {' ' * len(cid)}  = net {c.format(round(net))}")
        items = s["items_per_run"]
        for cat in ("bought", "crafted", "broken", "consumed", "lost on death", "granted", "sold", "dropped"):
            if cat in items:
                top = sorted(items[cat].items(), key=lambda t: -t[1])[:6]
                L.append(f"Items {cat} per run: " + ", ".join(f"{k} {v:.1f}" for k, v in top))
        L.append("Kills per run: " + ", ".join(f"{k} {v:.0f}" for k, v in list(s["kills_per_run"].items())[:8]))
        f = s["final"]
        L.append(f"Final (median): level {f['level_p50']:.0f}, money {cur.format(round(f['money_p50']))}, "
                 f"net worth {cur.format(round(f['worth_p50']))}; typical gear: "
                 + (", ".join(f"{k}={v}" for k, v in f["equipped_mode"].items()) or "none"))
        if log_lines and self.stats and self.stats[0].log:
            L.append("")
            L.append(f"Event log (run 0, first {log_lines} events):")
            L.extend("  " + x for x in self.stats[0].log[:log_lines])
        return "\n".join(L)


def _policy_dict(p: Policy) -> dict:
    return {
        "location": p.location, "max_death_risk": p.max_death_risk, "objective": p.objective,
        "sell_loot": p.sell_loot, "sell_outdated_gear": p.sell_outdated_gear, "buy_gear": p.buy_gear,
        "buy_consumables": p.buy_consumables, "use_consumables_below": p.use_consumables_below,
        "keep_consumables": p.keep_consumables,
        "craft": p.craft, "reserve": p.reserve, "shop_every": p.shop_every,
    }


def _policy_text(p: Policy, eco: Economy) -> str:
    parts = []
    if p.location == "auto":
        parts.append(f"location auto (death risk ≤ {p.max_death_risk:.1%}, maximise {p.objective})")
    else:
        parts.append(f"location {p.location}")
    parts.append("sell loot" if p.sell_loot else "keep loot")
    parts.append("buy gear" if p.buy_gear else "no gear purchases")
    if p.buy_consumables:
        parts.append(f"stock {p.buy_consumables} consumables (use below {p.use_consumables_below:.0%} odds)")
    parts.append(f"craft {p.craft}")
    if p.reserve:
        parts.append(f"reserve {eco.fmt(p.reserve)}")
    return ", ".join(parts)


def simulate(eco: Economy, n: int, runs: int = 50, seed: int = 1, every: int | None = None,
             policy: Policy | None = None, start: dict | None = None, trace=None, log: bool = False) -> SimResult:
    """Run `runs` independent simulations of `n` encounters each."""
    policy = policy or eco.policy
    every = every or max(1, n // 10)
    start = start or {}
    res = SimResult(eco, n, runs, seed, policy, "")
    for r in range(runs):
        sim = Simulator(eco, policy, seed=seed * 100_003 + r, snapshot_every=every,
                        log=log and r == 0, trace=trace, run_id=r)
        player = sim.new_player(level=start.get("level"), money=start.get("money"),
                                items=start.get("items"), location=start.get("location"))
        if r == 0:
            eq = ", ".join(sorted(player.equipped.values())) or "nothing equipped"
            inv = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in player.inv.items() if v > 0)
            res.start_desc = (f"level {player.level} at {player.location} with {eco.fmt(player.money, empty='no money')}; "
                              f"equipped: {eq}" + (f"; bag: {inv}" if inv else ""))
        res.stats.append(sim.run(n, player))
    return res
