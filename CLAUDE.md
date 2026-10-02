# Economy balancing harness — agent guide

This repo is a harness for balancing video-game economies. Economies are YAML files
(format: `docs/FORMAT.md`). Three tools give you feedback:

| command | what it tells you | speed |
|---|---|---|
| `uv run econ lint <file>` | exploits, difficulty (lifespans), pacing, sinks/faucets, + Lean-verified claims | ~1–3 s |
| `uv run econ sim <file> -n N` | how a simulated player evolves over N encounters (level, money, deaths, gear) | ~1 s per 100k encounters |
| `uv run econ tables <file>` | raw analytic tables: odds grid, lifespan/XP/income per location×level, drop values, shadow prices | instant |
| `uv run econ diff <old> <new>` | findings resolved/introduced and every key metric that moved, largest first | ~1 s |

Setup (already done on this machine): `uv sync`; Lean via `brew install elan-init && elan default stable`
then `cd lean && lake build` (the linter builds it on demand).

## The balancing loop

1. `uv run econ lint path/to/economy.yaml` (or a directory of YAML files).
2. Fix **errors** first — they are broken or exploitable (money pumps, death farming, unparseable values).
   Each finding has `fix:` with concrete numbers; applying any one listed fix removes that specific loop.
3. Then **warnings** — design-target misses (too hard/easy, too fast/slow, exploitable incentives).
4. **Info** findings are observations; act on them only if they contradict the design intent.
5. Re-run lint after every change. Changing one number often moves several metrics — keep a copy
   of the previous version and run `uv run econ diff old.yaml new.yaml` to see exactly what moved.
6. Use `econ sim` to confirm dynamics the analytic checks cannot see (gear churn, stalls, bankruptcies).
7. Finish when lint shows `0 errors · 0 warnings` (or every remaining warning is a deliberate,
   documented choice suppressed via `lint.ignore` with a `reason`).

`targets:` in the economy file is the design contract (lifespan range, even-fight odds, encounters
per level, upkeep ratio, wallet fill). Change the economy to meet targets; change targets only when
the *design intent* changes, and say so.

## Reading lint output

```
ERROR [money-pump] recipes.smelt  (examples/broken_economy.yaml:31)
  money pump (+8 gold per loop): buy copper_ore → craft smelt → sell copper_bar
    3× buy copper_ore: −15 gold, +3× copper_ore          ← the exact loop
    ...
  fix: Any one of: raise items.copper_ore.buy to ≥ 8 gold (now 5 gold); ...
  lean: verified by Lean kernel: pump3_is_pump          ← the exploit is formally confirmed
```

- `[code]` — `uv run econ explain <code>` explains the rule; `uv run econ explain` lists all rules.
- path + `file:line` point at the knob to change.
- `lean:` — the claim was re-checked by Lean's kernel (`lean/Generated/<Name>.lean`). For clean
  economies the `lean` info line lists e.g. *no money pump* (a proof over **all** possible trade
  loops) and the *income ceiling* (max shadow-worth gain per loot event).
- `--format json` gives the same data machine-readably (`findings[].data` has raw numbers).
- The **Metrics** section is the economy at a glance: shadow prices, odds by level difference, the
  difficulty map (death %, lifespan naked/geared, XP/encounter, encounters/level, income per
  location×level), spawn study, total levelling time, upkeep, and a quick simulation.

Key definitions:
- **lifespan** = expected encounters in one life (spawn → death, counting the fatal one) = 1/q
  for per-encounter death probability q. *naked* = no gear, *geared* = best obtainable gear allowed
  at that level. `lifespan-short` uses geared (too hard even with gear); `lifespan-long` uses
  naked (too easy even without gear).
- **spawn study** = exact expected lifespan for a player who spawns at the start of a location's
  range and stays there, levelling up during the life (dynamic program, no sampling).
- **shadow prices** = a consistent valuation under which no trade/craft creates value. Their
  existence *is* the proof that no money pump exists (Stiemke's lemma).

## Typical fixes

| problem | levers |
|---|---|
| money-pump / buy-below-sell | buy price ≥ value of what it can become; sell price < buy price; recipe `cost` |
| duplication | recipe outputs vs inputs (salvage/refine loops) |
| death-farming | make respawn grants unsellable (no `sell`), smaller, or add a flat death fee |
| penalty-evasion | widen buy/sell spread of store-of-value items, apply `respawn.inventory.lose_chance` |
| lifespan-short | lower enemy `level`/weights at the location, stronger/cheaper gear, battle `bias` up |
| lifespan-long | stronger enemies, lower win-probability ceiling in `battle.formula` |
| leveling-pace | `progression.xp_to_next`, enemy `xp`, `progression.xp_gain` |
| risk-reward-inversion | `xp_gain` that scales with `enemyLevel - playerLevel`, better rewards higher up |
| consumable-abuse | consumable `power`/`buy`, or flatten income jumps between zones |
| upkeep-heavy | gear `half_life`, gear prices, income at those levels |
| wallet-overflow / no-sink | `wallet_cap`, add things to buy or a death penalty for that currency |

## Simulating a specific player

```bash
uv run econ sim eco.yaml -n 2000 --runs 100                       # fresh players
uv run econ sim eco.yaml -n 500 --level 8 --location crypt \
    --money "5s" --items "iron_sword=1,health_potion=3"             # a specific player
uv run econ sim eco.yaml -n 500 --policy craft=none --policy max_death_risk=0.08
uv run econ sim eco.yaml -n 300 --runs 1 --log 80                  # narrated event log
uv run econ sim eco.yaml -n 1000 --trace out.csv                    # every encounter as data
uv run econ sim eco.yaml --format json                              # summary as JSON
```

The output shows the median [p10–p90] trajectory of level, effective level, money and net worth,
plus when each level and each piece of gear is first reached/owned, deaths, lifespans per life,
money flows (faucets vs sinks), item churn and kills. The bot
follows `player.policy` (auto location choice by risk/objective, sells loot, buys upgrades,
crafts, uses consumables). The bot will exploit money pumps if they exist — a money balance pinned
at the wallet cap is a red flag.

## Lean verification

`econ lint` (and `econ prove <file>` on its own) writes `lean/Generated/<Name>.lean`, which
instantiates the general model in `lean/EconomyModel/` with the concrete economy, and checks every
claim with `decide +kernel` (no `sorry`, no `native_decide`; only Lean's standard axioms):

- `no_money_pump` — from the exact shadow-price certificate (`Market.no_pump_of_prices`), or
  `pumpN_is_pump` for each reported exploit loop. Recipes with a `success_chance` count at their
  expected output here: a loop that pays off *on average* is an exploit;
- `income_ceiling` — whatever a player trades, crafts or rolls, one kill or respawn raises net worth
  (primary currency at face value) by at most `maxLootValue` (`Market.income_ceiling_of_valuation`,
  via value conservation `Market.worth_run`). It is stated over `luckyMarket`, where every chancy
  recipe succeeds, so it holds for any luck; respawn currency formulas count as a faucet;
- `<location>_L<level>_<geared|naked>_life` — the lifespan claims behind lifespan findings
  (`EncounterTable`, `lifespan = 1/q`, geometric survival model in `Lifespan.lean`);
- `<location>_<loadout>_monotone` — levelling up never lowers any win probability there;
- `fastest_<item>` — no strategy, however lucky, owns `<item>` before N kills/respawns from
  `player.start` (`Market.grind_lower_bound`: a valuation + value conservation). Shown in the metrics
  as *Fastest possible acquisition*; compare with the simulator's *Gear first owned* (typical timing).

A `lean-mismatch` error means Lean rejected a claim the Python analysis made — that is a harness
bug, not an economy problem. `--no-lean` skips Lean for faster iteration; results are cached by
content hash.

## Repo layout

```
src/econ/
  model.py         YAML schema (pydantic)          economy.py   compile → Economy (resolved, typed)
  loader.py        YAML loading, dir merge, lines  money.py / formula.py  money & safe formulas
  analysis/        market.py (LP pumps/certificates), combat.py (lifespan, spawn DP, income),
                   respawn.py (death farming, penalty evasion)
  lint/            findings.py (rule registry/docs), rules.py (checks), runner.py, report.py
  proofs/          generate.py (Lean module), runner.py (lake, caching, result mapping)
  sim/             engine.py (player, policy, encounters), report.py (aggregation/output)
lean/EconomyModel/ Sum, Market (no-pump theorem), Conservation (income ceiling), Lifespan
examples/          starter_valley.yaml (balanced), broken_economy.yaml (every exploit)
tests/             uv run pytest
```

To add a lint rule: register it in `lint/findings.py` (`rule(...)` gives `econ explain` docs),
implement a check in `lint/rules.py` (`ctx.add(code, path, message, detail=, fix=)`), add it to
`ALL_CHECKS`, and add a case to `tests/`. To add a Lean claim: extend `proofs/generate.py` and, if
needed, prove a general theorem in `lean/EconomyModel/` (`cd lean && lake build`).
