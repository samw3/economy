# Economy file format

An economy is described in YAML: either one file, or a directory of `.yaml` files that are
merged (map sections such as `items` merge by id; single sections such as `battle` may appear
once). `econ schema` prints the JSON Schema; `econ new my.yaml` writes a commented template;
[`examples/starter_valley.yaml`](../examples/starter_valley.yaml) is a complete worked example.

```yaml
meta:        # name, encounter rate
params:      # constants for formulas
currencies:  # denominations, wallet caps
items:       # buy/sell prices, gear, consumables, usage half-life
recipes:     # crafting / refining / salvage / exchange, with optional extra cost
enemies:     # level, XP, drops and drop rates
locations:   # encounter tables, intended player levels
battle:      # P(win) = f(playerLevel, enemyLevel)
progression: # max level, XP curve
respawn:     # what happens to a defeated player
player:      # starting state + simulated player policy
targets:     # design goals the linter checks
lint:        # suppressed findings
```

Unknown fields are errors (typos such as `hal_life` are caught with a "did you mean" hint).

## Model in one paragraph

Encounters happen at a fixed rate (`meta.encounter_rate` per minute). At each encounter, an enemy
is drawn from the current location's table, and the player wins with probability
`battle.formula(playerLevel, enemyLevel)` where `playerLevel` is the *effective* level: character
level + power of equipped gear (+ a consumable's power if one is used for this fight). A win
awards XP and rolls the enemy's drops. A loss means death: the `respawn` rules transform the
player's state. Equipped gear wears out: after every encounter each equipped item breaks with
probability `1 − 0.5^(1/half_life)`. Between encounters players trade with vendors
(buy/sell prices), craft recipes and move between locations.

## Money

All amounts are integers in a currency's **base unit** (its value-1 denomination). Write them as:

| form | example | meaning |
|---|---|---|
| number | `150` | 150 base units of the primary currency |
| denominated | `"1g 50s 3c"`, `1g50s`, `1.5g`, `"2 silver"` | uses denomination symbols or names |
| currency-suffixed | `"5 gem"` | 5 base units of currency `gem` |
| mapping | `{gem: 5, coin: 2s}` | multi-currency price |

## `meta`

| field | default | |
|---|---|---|
| `name` | `"Unnamed economy"` | |
| `encounter_rate` | `1.0` | encounters per minute of play; converts encounters ↔ time |

## `params`

`{name: number}` constants available in every formula (e.g. `bias: 3.0`).

## `currencies`

```yaml
currencies:
  coin:
    denominations:              # optional; exactly one must have value 1
      - {name: copper, symbol: c, value: 1}
      - {name: silver, symbol: s, value: 100}
      - {name: gold,   symbol: g, value: 10000}
    wallet_cap: 20g             # optional; excess income is lost
    primary: true               # default currency for bare numbers (default: first listed)
  gem: {wallet_cap: 99}
```

Symbols and names must be unique across currencies. Currency ids can be used as units
(`"5 gem"`).

## `items`

```yaml
items:
  iron_sword: {buy: 8s, sell: 2s, slot: weapon, power: 3, half_life: 300, min_level: 6}
  potion:     {buy: 40c, sell: 5c, consumable: true, power: 1.5}
  iron_ore:   {sell: 10c}
```

| field | default | |
|---|---|---|
| `buy` | – | vendor price; omit if vendors don't sell it |
| `sell` | – | price vendors pay; omit if unsellable |
| `slot` | – | equipment slot; makes the item **gear** (best item per slot is equipped) |
| `power` | `0` | bonus effective levels while equipped (gear) or for one battle (consumable) |
| `half_life` | ∞ (consumables: 0) | **usage half-life**: number of uses after which there is a 50% chance the item has broken. One use = one encounter while equipped, or one activation of a consumable. `0` = destroyed on first use |
| `min_level` | `1` | level required to equip/use |
| `consumable` | `false` | used for a single battle when the odds look bad (see policy) |
| `tags`, `description` | | free-form |

Per-use break chance is `1 − 0.5^(1/half_life)`; expected uses ≈ `half_life / ln 2`.

## `recipes`

Crafting, refining, salvage and currency exchange are all recipes:

```yaml
recipes:
  smelt_iron:
    inputs: {iron_ore: 3}          # item -> quantity, or currency -> amount
    outputs: {iron_bar: 1}
    cost: 5c                       # additional crafting cost
    success_chance: 1.0            # inputs and cost are consumed even on failure
    min_level: 1
  sell_gem:                        # exchange
    inputs: {gem: 1}
    outputs: {coin: 1s}
```

## `enemies`

```yaml
enemies:
  wolf:
    level: 4
    xp: 14
    drops:                                   # independent rolls on each kill
      - {currency: coin, amount: [4, 10]}    # amount: money or [min, max] uniform
      - {item: wolf_pelt, qty: 1, chance: 0.4}
      - {item: bone, qty: [1, 2], chance: 0.7}
```

## `locations`

```yaml
locations:
  town: {safe: true}                 # no encounters
  forest:
    level_range: [3, 6]              # player levels it is designed for (drives lint targets)
    encounter_rate: 2                # optional override of meta.encounter_rate
    enemies:                         # encounter table
      - {enemy: wolf, weight: 60, level: [3, 5]}   # level override: n or [min, max] uniform
      - {enemy: rat,  weight: 25, level: 3}
```

## `battle`

```yaml
battle:
  formula: clamp(sigmoid((delta + bias) / spread), 0.005, 0.995)
  params: {bias: 3, spread: 1.1}     # optional, local constants
```

Returns the probability that the player wins. Variables: `playerLevel` (effective level),
`enemyLevel`, `delta = playerLevel - enemyLevel`. The engine clamps to [0, 1], but the linter
reports formulas that leave that range, are not monotone, or make fights unwinnable/unlosable.

## `progression`

```yaml
progression:
  max_level: 15
  xp_to_next: floor(60 * level ^ 1.7)      # XP from `level` to `level+1`; or a list [60, 194, ...]
  xp_gain: xp * clamp(1 + 0.15 * (enemyLevel - playerLevel), 0.1, 1.5)   # default: xp
```

`xp_gain` variables: `xp` (enemy base XP), `playerLevel` (character level), `enemyLevel`, `delta`.

## `respawn`

How a defeated player's stats and inventory change:

```yaml
respawn:
  location: town                 # default: where they died
  level: level                   # vars: level, xp, deaths          (result clamped to 1..max)
  xp: floor(xp * 0.5)            # vars: level, xp, deaths, xpToNext
  currencies:
    coin: floor(amount * 0.85)   # vars: amount, cap, level, deaths  (unlisted currencies kept)
  inventory: {lose_chance: 0.1, protect: [quest_key]}   # each unequipped unit
  equipped:  {lose_chance: 0.0, protect: []}            # each equipped item
  grants:
    - {item: training_sword, qty: 1, if_missing: true}  # handed out on respawn
    - {currency: coin, amount: 20c, if_missing: true}   # tops up to 20c
```

## `player`

```yaml
player:
  start:
    level: 1
    location: meadow             # default: lowest-level combat location
    currencies: {coin: 60c}
    items: {wooden_sword: 1, health_potion: 2}
    equipped: [wooden_sword]     # default: best owned gear
  policy:                        # the simulated player's behaviour between encounters
    location: auto               # or a location id
    max_death_risk: 0.03         # auto: best location whose death chance/encounter ≤ this
    objective: xp                # xp | income | balanced
    sell_loot: true              # sell non-gear, non-consumable loot (keeps 2× recipe inputs)
    sell_outdated_gear: true
    buy_gear: true               # buy the best affordable upgrade per slot
    buy_consumables: 2           # keep this many in stock (0 = never buy)
    keep_consumables: 5          # sell dropped consumables beyond this
    use_consumables_below: 0.6   # use one when P(win) is below this
    craft: profitable            # none | profitable | gear | all
    reserve: 0                   # money never spent when shopping
    shop_every: 10               # encounters between shopping trips
```

## `targets`

Design goals. Ranges are `[min, max]` (either may be `null`) or `{min: , max: }`; `null` disables.

| field | default | checked by |
|---|---|---|
| `lifespan` | `[15, 400]` | `lifespan-short` / `lifespan-long` (encounters per life) |
| `even_fight_win` | `[0.5, 0.9]` | `even-fight-odds` |
| `encounters_per_level` | `[10, 400]` | `leveling-pace` |
| `hours_to_max_level` | `null` | `leveling-pace` (total play time to max level at the intended locations) |
| `max_upkeep_ratio` | `0.5` | `upkeep-heavy` (gear upkeep / income) |
| `wallet_fill_encounters` | `50` | `wallet-overflow` |

## `lint`

```yaml
lint:
  ignore:
    - dead-item                                   # every finding with this code
    - {code: unused, path: enemies.ghost, reason: "boss added in v2"}
```

## Formulas

Arithmetic expressions: numbers, variables, `+ - * / // % **` (`^` also means power),
comparisons, `and`/`or`/`not`, `a if cond else b`, and the functions `min max abs floor ceil round
sqrt exp log log2 log10 pow clamp(x, lo, hi) sigmoid(x) lerp(a, b, t) smoothstep(e0, e1, x)`.
`econ explain formulas` lists the variables available in each context.
