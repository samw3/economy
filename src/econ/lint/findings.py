"""Findings (diagnostics) and the documented rule registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "info"]
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str
    path: str = ""
    detail: str = ""
    fix: str = ""
    file: str | None = None
    line: int | None = None
    lean: str | None = None  # e.g. "proved no_money_pump", "proved pump_1 (exploit is real)"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
        }
        if self.file:
            d["file"] = self.file
        if self.line:
            d["line"] = self.line
        if self.detail:
            d["detail"] = self.detail
        if self.fix:
            d["fix"] = self.fix
        if self.lean:
            d["lean"] = self.lean
        if self.data:
            d["data"] = self.data
        return d


def fmt_path(path) -> str:
    """('locations', 'meadow', 'enemies', 1, 'weight') -> locations.meadow.enemies[1].weight"""
    if isinstance(path, str):
        return path
    out = ""
    for p in path:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out


@dataclass(frozen=True)
class Rule:
    code: str
    severity: Severity
    category: str
    summary: str
    explanation: str
    fix: str


RULES: dict[str, Rule] = {}


def rule(code, severity, category, summary, explanation, fix):
    RULES[code] = Rule(code, severity, category, summary, explanation.strip(), fix.strip())


# --- structure ---------------------------------------------------------------------------
rule("yaml", "error", "structure", "File is not valid YAML.",
     "The economy file could not be parsed as YAML.",
     "Fix the YAML syntax at the reported line (indentation, unclosed quotes/brackets).")
rule("schema", "error", "structure", "A field has the wrong shape or an unknown name.",
     "Every section of the economy format has a fixed set of fields (see docs/FORMAT.md or "
     "`econ schema`). Unknown fields are rejected so that typos (e.g. `hal_life`) do not "
     "silently fall back to defaults.",
     "Rename the field to the suggested name, or remove it.")
rule("missing-section", "error", "structure", "A required section is missing.",
     "`currencies`, `enemies`, `locations`, `battle` and `progression` are required.",
     "Add the section (run `econ new` for a template).")
rule("duplicate-definition", "error", "structure", "The same id is defined twice.",
     "When linting a directory, every item/enemy/location/... id must be unique across files, "
     "and single-valued sections (battle, progression, ...) may appear only once.",
     "Rename or delete one of the definitions.")
rule("unknown-reference", "error", "structure", "A reference points to an undefined id.",
     "Items, currencies, enemies and locations are referenced by id. A reference to an id that "
     "is not defined is almost always a typo.",
     "Use one of the suggested ids, or define the missing entry.")
rule("bad-money", "error", "structure", "A money amount cannot be parsed.",
     "Money is written as a number of base units (`150`), a denominated string (`1g 50s`), a "
     "currency-suffixed string (`5 gem`) or a mapping (`{gem: 5, coin: 2s}`).",
     "Use one of the accepted forms with a known denomination symbol/name or currency id.")
rule("bad-formula", "error", "structure", "A formula is invalid or fails to evaluate.",
     "Formulas are arithmetic expressions over the documented variables. They are checked at "
     "compile time and evaluated on a grid of inputs to catch division by zero, domain errors "
     "and similar problems.",
     "Fix the expression; see `econ explain formulas` for variables and functions.")
rule("bad-value", "error", "structure", "A value is out of its allowed range.",
     "Some numbers have natural bounds: chances in [0, 1], positive weights, ranges with "
     "min ≤ max, levels within 1..max_level, and so on.",
     "Change the value to lie within the stated bounds.")

# --- exploits ----------------------------------------------------------------------------
rule("money-pump", "error", "exploit", "A trade/craft loop creates value from nothing.",
     "Some combination of buying, selling, crafting, refining and exchanging consumes nothing "
     "on net but produces currency or a valuable item. Players can repeat it forever, so it "
     "breaks every other balance consideration. The linter finds such loops with linear "
     "programming (Stiemke's lemma: either consistent shadow prices exist or a pump does) and "
     "Lean's kernel re-checks the loop (`pump_*` theorems) or the absence of any loop "
     "(`no_money_pump`).",
     "Break the loop: make the buy price ≥ what the output sells for, raise the crafting cost, "
     "or lower the sell price. The finding lists the exact amount each knob must move.")
rule("duplication", "error", "exploit", "A loop duplicates items without consuming anything.",
     "A combination of recipes/trades returns more of some valuable item than it consumes. "
     "Even if the item cannot be sold, unlimited copies trivialise anything it is used for.",
     "Check recipe outputs (especially salvage/refine recipes) against their inputs.")
rule("free-item", "warning", "exploit", "A valuable item can be bought for nothing.",
     "An item with a gameplay effect or a crafting use has a buy price of 0, so players can "
     "take unlimited copies.",
     "Give the item a non-zero buy price or remove it from the vendor.")
rule("death-farming", "error", "exploit", "Dying is profitable for some players.",
     "Respawning grants items or currency worth more than the death penalty takes from a "
     "player with little money. Such players can sell the grant, die on purpose and repeat.",
     "Make grants unsellable (no `sell` price), reduce their value, add a flat death fee, or "
     "only grant when the player owns no equivalent gear.")
rule("penalty-evasion", "warning", "exploit", "The death penalty can be dodged by holding items.",
     "The respawn penalty takes a share of currency but items are kept. If an item can be "
     "bought and sold back with a smaller loss than the penalty, players park their money in "
     "it before risky fights.",
     "Also apply losses to inventory, widen the buy/sell spread of store-of-value items, or "
     "lower the currency penalty.")
rule("risk-reward-inversion", "warning", "exploit", "An easier location pays better.",
     "For players at some level, a location with lower death risk also gives more XP and more "
     "income than the location designed for that level, so players have no reason to progress "
     "(or the harder location is pointless).",
     "Raise rewards of the intended location, lower rewards of the easier one, or reduce XP for "
     "over-levelled players with a `progression.xp_gain` formula.")
rule("safe-farm", "warning", "exploit", "A near-riskless high-income farming spot exists.",
     "Some enemy gives far more value per encounter than others for the risk involved, so "
     "optimal players will farm it exclusively.",
     "Lower its drop rates/values or raise its level.")
rule("consumable-abuse", "warning", "exploit", "Consumables make hard enemies a profitable farm.",
     "A consumable's power boost turns a high-value, high-risk enemy into a safe kill, and the "
     "expected loot pays for the consumable with margin to spare.",
     "Reduce the consumable's power, raise its price, or lower that enemy's drops.")

# --- difficulty / lifespan ---------------------------------------------------------------
rule("lifespan-short", "warning", "difficulty", "Players die too quickly: the game is too hard here.",
     "Expected encounters per life (from spawn until death) is below `targets.lifespan` for "
     "players within the location's `level_range`, even with the best gear available at their "
     "level. Lifespan is 1/q encounters where q is the per-encounter death probability; the "
     "levelling spawn study also accounts for level-ups during the life.",
     "Lower enemy levels/weights, raise `level_range`, improve the battle formula's odds, or "
     "make gear stronger/cheaper.")
rule("lifespan-long", "warning", "difficulty", "Players almost never die: the game is too easy here.",
     "Expected encounters per life exceeds `targets.lifespan` even for players without any gear.",
     "Add stronger enemies, lower `level_range`, or make the battle formula less forgiving.")
rule("immortal", "warning", "difficulty", "Some fights can never be lost.",
     "The battle function returns exactly 1 for matchups that occur in the game, so the "
     "per-encounter death probability can be 0 and lifespans are infinite.",
     "Cap the win probability below 1 (e.g. `clamp(..., 0.02, 0.98)`).")
rule("hopeless", "warning", "difficulty", "Some fights can never be won.",
     "The battle function returns 0 for matchups that occur within a location's intended "
     "level range.",
     "Give underdogs a small floor, e.g. `clamp(..., 0.02, 0.98)`.")
rule("odds-range", "error", "difficulty", "The battle function leaves [0, 1].",
     "The battle formula must return a probability. Values outside [0, 1] are clamped by the "
     "engine, which usually hides a formula mistake.",
     "Wrap the formula in `clamp(..., 0, 1)` or fix its scale.")
rule("odds-not-monotone", "warning", "difficulty", "Higher levels can lower the odds of winning.",
     "The battle function should increase with playerLevel and decrease with enemyLevel. "
     "Otherwise levelling up or gearing up can make the game harder.",
     "Fix the formula so it is monotone in both arguments.")
rule("even-fight-odds", "warning", "difficulty", "Odds of an even fight are off target.",
     "The win probability when playerLevel == enemyLevel is outside `targets.even_fight_win`.",
     "Shift the battle formula (e.g. its bias parameter).")
rule("death-spiral", "warning", "difficulty", "Respawned players cannot survive anywhere.",
     "After the respawn penalty, a player's expected lifespan in every location they can enter "
     "is below the target minimum, so deaths cause more deaths.",
     "Make the respawn penalty lighter on gear/levels, or add an easier location.")

# --- progression -------------------------------------------------------------------------
rule("level-gap", "warning", "progression", "No location is designed for some levels.",
     "Some player levels between 1 and `progression.max_level` are not inside any location's "
     "`level_range`.",
     "Extend a location's `level_range` or add a location for those levels.")
rule("leveling-pace", "warning", "progression", "Levelling is too fast or too slow.",
     "Encounters needed to gain a level at the intended location(s) are outside "
     "`targets.encounters_per_level`.",
     "Tune `progression.xp_to_next`, enemy `xp`, or `progression.xp_gain`.")
rule("location-dominated", "info", "progression", "A location is strictly worse than another.",
     "Another location is at least as safe and gives at least as much XP and income for every "
     "level in this location's range.",
     "Differentiate it (better drops, unique materials) or remove it.")

# --- currency / items --------------------------------------------------------------------
rule("unaffordable", "error", "economy", "Something costs more than the wallet can hold.",
     "A buy price or crafting cost exceeds the currency's `wallet_cap`, so it can never be paid.",
     "Raise the wallet cap or lower the price.")
rule("wallet-overflow", "warning", "economy", "Loot overflows the wallet quickly.",
     "At the intended levels the wallet cap is reached within a few encounters' worth of "
     "income, after which currency drops are wasted.",
     "Raise `wallet_cap`, add currency sinks, or lower currency drops.")
rule("no-sink", "warning", "economy", "A currency can be earned but never spent.",
     "Nothing costs this currency (no buy price, crafting cost or respawn penalty), so it "
     "accumulates without limit: pure inflation.",
     "Give it a use: items to buy, crafting fees, or a death penalty.")
rule("no-source", "info", "economy", "A currency can never be obtained.",
     "No enemy drops it, nothing sells for it, no recipe produces it and the player does not "
     "start with it.",
     "Add a source, or remove the currency.")
rule("unobtainable-item", "warning", "economy", "An item can never be obtained.",
     "It cannot be bought, is not dropped by any enemy that appears in a location, is not "
     "produced by any obtainable recipe and is never granted.",
     "Add a source or remove the item.")
rule("dead-item", "info", "economy", "An item has no use.",
     "It cannot be sold, equipped, consumed or used in a recipe. It only clutters inventories.",
     "Give it a sell price or a recipe use, or remove it.")
rule("unused", "info", "economy", "A definition is never used.",
     "An enemy that appears in no location, or a recipe whose inputs can never be obtained.",
     "Place the enemy in a location, or remove the definition.")
rule("buy-below-sell", "error", "exploit", "An item sells for more than it costs.",
     "Buying the item and immediately selling it back is profitable.",
     "Make `sell` lower than `buy`.")
rule("dominated-gear", "info", "economy", "A gear item is strictly worse than another in its slot.",
     "Another item in the same slot has at least the power, at most the price and level "
     "requirement, and at least the durability.",
     "Differentiate or remove the dominated item.")
rule("upkeep-heavy", "warning", "economy", "Gear wears out faster than players can pay for it.",
     "Expected replacement cost per encounter of the best gear for a level (buy price × "
     "break chance per use) exceeds `targets.max_upkeep_ratio` × income per encounter.",
     "Increase half-lives, lower gear prices, or raise income at those levels.")
rule("crafting-unprofitable", "info", "economy", "A recipe always loses money.",
     "The recipe's outputs are worth less than buying its inputs plus its cost, and the "
     "outputs can be bought more cheaply directly. Nobody will use it.",
     "Lower the cost or make outputs more valuable (or not purchasable).")

# --- simulation / lean ---------------------------------------------------------------------
rule("progression-stall", "warning", "simulation", "Simulated players stop levelling.",
     "In the quick simulation, the median player's level stays flat for a long stretch before "
     "reaching max level.",
     "Check the location covering that level and the gear/income needed to survive there.")
rule("bankrupt", "warning", "simulation", "Simulated players end up broke.",
     "The median simulated player's money trends to zero: costs (gear upkeep, death penalties) "
     "exceed income.",
     "Lower upkeep/penalties or raise income.")
rule("lean", "info", "verification", "Result of the Lean verification step.",
     "The linter generates a Lean module encoding the economy and asks Lean's kernel to check "
     "the claims behind exploit/lifespan findings.",
     "If Lean reports a failure the Python analysis and the Lean model disagree; please report it.")
rule("lean-unavailable", "info", "verification", "Lean verification was skipped.",
     "Lean (elan/lake) is not installed or the model library failed to build.",
     "Install with `brew install elan-init && elan default stable`, then run `econ prove`.")
rule("lean-mismatch", "error", "verification", "Lean could not verify a claim made by the linter.",
     "A theorem generated from the linter's analysis failed to check in Lean. Either the "
     "analysis has a bug or numeric precision broke an exact claim.",
     "Inspect the generated Lean file listed in the finding.")
rule("internal-error", "warning", "verification", "An analysis crashed.",
     "One of the linter's analyses raised an exception. Other findings are still reported.",
     "Please report the traceback in the finding's detail.")
