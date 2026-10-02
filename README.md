# econ — a game-economy balancing harness

Describe an RPG-style economy in YAML, then let an agent (or a person) iterate on it with three
kinds of feedback:

1. **A linter** that finds exploits and imbalances — money pumps, item duplication, death farming,
   penalty evasion, risk/reward inversions, too-short or too-long player lifespans, bad levelling
   pace, missing sinks — with exact numbers and concrete fixes.
2. **Lean proofs**: the linter writes a Lean 4 module encoding the economy and has Lean's kernel
   check its claims — e.g. *no combination of trades and recipes can create value* (or the
   reported exploit loop is real), an *income ceiling* per kill that holds whatever the luck,
   lower bounds on how fast any item can be obtained, and every lifespan claim.
3. **A Monte Carlo simulator** that plays N encounters for a player (or many players) under a
   configurable policy and reports how they evolve.

```bash
uv sync
uv run econ lint examples/starter_valley.yaml      # clean, balanced example
uv run econ lint examples/broken_economy.yaml      # every exploit the linter knows
uv run econ sim  examples/starter_valley.yaml -n 2000 --runs 100
uv run econ prove examples/broken_economy.yaml     # just the Lean step, theorem by theorem
uv run econ tables examples/starter_valley.yaml    # odds / lifespan / XP / prices tables
uv run econ diff old.yaml new.yaml                 # what a change fixed, broke and moved
uv run econ explain money-pump                     # docs for any rule (or: formulas, format)
uv run econ new my_economy.yaml                    # commented template
uv run econ schema > economy.schema.json           # JSON Schema for editors
```

Requirements: [uv](https://docs.astral.sh/uv/) (Python ≥ 3.12) and, for the proofs,
`brew install elan-init && elan default stable` (Lean 4; core only, no Mathlib — checks take ~1 s).

## The model

- **Encounters at a fixed rate** (`meta.encounter_rate`/min). Each draws an enemy from the
  location's weighted table.
- **Battle function** `P(win) = f(playerLevel, enemyLevel)` — any formula; `playerLevel` includes
  gear and consumable power.
- **Respawn function** — formulas for level, XP and each currency after death, item loss chances
  for inventory/equipment, and grants.
- **Enemies** with levels, XP and drop tables (items/currencies, quantities, chances).
- **Currencies** with denominations (`1g 50s 3c`) and wallet caps.
- **Items** with buy/sell prices and a **usage half-life** (uses until a 50% chance it broke).
- **Recipes** (crafting, refining, salvage, exchange) with extra cost and success chance.

See [docs/FORMAT.md](docs/FORMAT.md) for the full reference and
[CLAUDE.md](CLAUDE.md) for the agent workflow.

## How the analysis works

| question | method |
|---|---|
| Is there any trade/craft/exchange loop that creates value? | Linear programming over all buy/sell/recipe actions (chancy recipes at their expected output). By Stiemke's lemma either strictly positive *shadow prices* exist under which no action gains value (a certificate: no loop exists, ever) or a non-negative integer combination of actions gains something from nothing (an explicit exploit). The LP solution is converted to exact rationals and checked by Lean. |
| How fast can wealth grow? | Under a valuation of the *lucky* market (every chancy recipe succeeds), value is conserved by trading, so worth grows only through faucets (drops, respawn grants and respawn formulas): Lean proves an income ceiling per kill/respawn that holds whatever the luck. |
| Can players skip content? | For each item, an LP finds the valuation that maximises (item price − start worth) / (best possible loot event); Lean proves that no strategy, however lucky, owns the item in fewer faucet events. The simulator reports when gear is typically first owned. |
| How long does a player live per spawn? | Per-encounter death probability q from the encounter table and battle function → lifespan 1/q (geometric law, formalised in Lean), naked vs. best gear; plus an exact dynamic program for a spawn that levels up during its life. |
| Is levelling paced well? Are zones ordered sensibly? | XP and income per encounter by location and level, compared with targets and with neighbouring zones. |
| Is dying ever profitable / avoidable? | Respawn formulas scanned over all wallet amounts against the value of grants; store-of-value spreads vs. the penalty. |
| What actually happens over 10 hours of play? | The simulator: a bot that picks locations by risk, sells loot, buys upgrades, crafts and uses consumables. |

## Layout

```
src/econ/          Python package (CLI: econ)
lean/              Lean 4 project: EconomyModel library + Generated/ per-economy modules
examples/          starter_valley.yaml, broken_economy.yaml
docs/FORMAT.md     format reference
tests/             pytest suite (uv run pytest)
```
