"""Raw schema of economy files (validated with pydantic).

This layer only checks *shape*: field names and basic types. Semantic validation (money
amounts, formulas, references between sections) happens when the file is compiled into an
`econ.economy.Economy`, where errors can be reported with economy-specific hints.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator

MONEY_DOC = (
    "Money: a number of base units (150), a denominated string ('1g 50s 3c'), a "
    "currency-suffixed string ('5 gem') or a mapping ({gem: 5, coin: '2s'})."
)
FORMULA_DOC = "Formula: an arithmetic expression string (see `econ explain formulas`) or a number."
RANGE_DOC = "A single integer or an inclusive [min, max] range."

Money = Annotated[
    Any,
    WithJsonSchema({
        "anyOf": [
            {"type": "integer", "minimum": 0},
            {"type": "string"},
            {"type": "object", "additionalProperties": {"type": ["integer", "string"]}},
        ],
        "description": MONEY_DOC,
    }),
]
FormulaSrc = Annotated[
    Any,
    WithJsonSchema({"type": ["string", "number"], "description": FORMULA_DOC}),
]
IntRange = Annotated[
    Any,
    WithJsonSchema({
        "anyOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}],
        "description": RANGE_DOC,
    }),
]
AmountRange = Annotated[
    Any,
    WithJsonSchema({
        "anyOf": [
            {"type": "integer"}, {"type": "string"},
            {"type": "array", "items": {"type": ["integer", "string"]}, "minItems": 2, "maxItems": 2},
        ],
        "description": "A money amount or an inclusive [min, max] range of money amounts.",
    }),
]
TargetRange = Annotated[
    Any,
    WithJsonSchema({
        "anyOf": [
            {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 2, "maxItems": 2},
            {"type": "object", "properties": {"min": {"type": "number"}, "max": {"type": "number"}}},
            {"type": "null"},
        ],
        "description": "[min, max] (either may be null), {min: x, max: y}, or null to disable.",
    }),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _none_to_empty(v, default):
    return default if v is None else v


class MetaDef(Strict):
    name: str = Field("Unnamed economy", description="Display name of the economy.")
    version: Union[int, float, str, None] = None
    description: str | None = None
    encounter_rate: float = Field(
        1.0, gt=0,
        description="Encounters per minute of play. Encounters happen at this fixed rate; it "
                    "converts encounter counts into play time.")


class DenominationDef(Strict):
    name: str = Field(description="Denomination name, e.g. 'silver'.")
    symbol: str | None = Field(None, description="Short symbol used in amounts, e.g. 's'. Defaults to the name.")
    value: int = Field(gt=0, description="Value in base units. Exactly one denomination must have value 1.")


class CurrencyDef(Strict):
    description: str | None = None
    denominations: list[DenominationDef] = Field(
        default_factory=list,
        description="Coin denominations, e.g. copper=1, silver=100, gold=10000. Optional.")
    wallet_cap: Money = Field(None, description="Maximum amount a player can hold; excess loot is lost. " + MONEY_DOC)
    primary: bool = Field(False, description="Default currency for bare numbers. Defaults to the first currency.")

    @field_validator("denominations", mode="before")
    @classmethod
    def _dn(cls, v):
        return _none_to_empty(v, [])


class ItemDef(Strict):
    description: str | None = None
    buy: Money = Field(None, description="Vendor price to buy one unit. Omit if vendors do not sell it. " + MONEY_DOC)
    sell: Money = Field(None, description="Vendor price paid to the player for one unit. Omit if unsellable.")
    slot: str | None = Field(None, description="Equipment slot (weapon, armor, ...). Makes the item gear.")
    power: float = Field(0.0, description="Effective-level bonus while equipped (gear) or for one battle (consumables).")
    half_life: float | None = Field(
        None, ge=0,
        description="Usage half-life: number of uses after which there is a 50% chance the item has "
                    "broken. A use is one encounter while equipped, or one activation of a consumable. "
                    "0 = destroyed on first use; omit = indestructible (consumables default to 0).")
    min_level: int = Field(1, ge=1, description="Level required to equip/use.")
    consumable: bool = Field(False, description="Used up when activated for one battle.")
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        return _none_to_empty(v, [])


class RecipeDef(Strict):
    description: str | None = None
    inputs: dict[str, Union[int, float, str]] = Field(
        default_factory=dict,
        description="Consumed goods: item id -> quantity, or currency id -> money amount.")
    outputs: dict[str, Union[int, float, str]] = Field(
        default_factory=dict, description="Produced goods (same form as inputs).")
    cost: Money = Field(None, description="Additional crafting fee. " + MONEY_DOC)
    success_chance: float = Field(1.0, gt=0, le=1, description="Chance outputs are produced; inputs and cost are always consumed.")
    min_level: int = Field(1, ge=1, description="Player level required to use the recipe.")

    @field_validator("inputs", "outputs", mode="before")
    @classmethod
    def _io(cls, v):
        return _none_to_empty(v, {})


class DropDef(Strict):
    item: str | None = Field(None, description="Dropped item id (use this or `currency`).")
    currency: str | None = Field(None, description="Dropped currency id.")
    qty: IntRange = Field(None, description="Item quantity: n or [min, max] (uniform). Default 1.")
    amount: AmountRange = Field(None, description="Currency amount: money or [min, max] (uniform).")
    chance: float = Field(1.0, ge=0, le=1, description="Probability this drop happens on a kill.")


class EnemyDef(Strict):
    description: str | None = None
    level: int = Field(ge=1, description="Default enemy level (locations may override).")
    xp: float = Field(0, ge=0, description="Base XP for defeating the enemy (see progression.xp_gain).")
    drops: list[DropDef] = Field(default_factory=list, description="Independent drop rolls on a kill.")
    tags: list[str] = Field(default_factory=list)

    @field_validator("drops", "tags", mode="before")
    @classmethod
    def _lists(cls, v):
        return _none_to_empty(v, [])


class EncounterDef(Strict):
    enemy: str = Field(description="Enemy id.")
    weight: float = Field(1.0, gt=0, description="Relative frequency of this enemy at the location.")
    level: IntRange = Field(None, description="Enemy level here: n or [min, max] (uniform). Defaults to the enemy's level.")


class LocationDef(Strict):
    description: str | None = None
    level_range: IntRange = Field(None, description="Player levels this location is designed for, [min, max].")
    encounter_rate: float | None = Field(None, gt=0, description="Overrides meta.encounter_rate here.")
    safe: bool = Field(False, description="No encounters (towns). Respawn points are usually safe.")
    enemies: list[EncounterDef] = Field(default_factory=list, description="Encounter table.")

    @field_validator("enemies", mode="before")
    @classmethod
    def _en(cls, v):
        return _none_to_empty(v, [])


class BattleDef(Strict):
    description: str | None = None
    formula: FormulaSrc = Field(
        description="Probability that the player wins. Variables: playerLevel (effective level incl. "
                    "gear and consumables), enemyLevel, delta = playerLevel - enemyLevel, plus params.")
    params: dict[str, float] = Field(default_factory=dict, description="Named constants for the formula.")


class ProgressionDef(Strict):
    max_level: int = Field(ge=1)
    xp_to_next: Union[list[int], FormulaSrc] = Field(
        description="XP needed to go from `level` to `level + 1`: a formula of `level` or a list "
                    "(first entry = level 1 -> 2).")
    xp_gain: FormulaSrc = Field(
        "xp", description="XP awarded per win. Variables: xp (enemy base xp), playerLevel, enemyLevel, delta.")


class LossDef(Strict):
    lose_chance: float = Field(0.0, ge=0, le=1, description="Each unit is lost with this probability on death.")
    protect: list[str] = Field(default_factory=list, description="Item ids that are never lost.")

    @field_validator("protect", mode="before")
    @classmethod
    def _p(cls, v):
        return _none_to_empty(v, [])


class GrantDef(Strict):
    item: str | None = None
    currency: str | None = None
    qty: int | None = Field(None, ge=1, description="Item quantity (default 1).")
    amount: Money = Field(None, description="Currency amount.")
    if_missing: bool = Field(False, description="Only grant if the player has none (items) / less than the amount (currency).")


class RespawnDef(Strict):
    description: str | None = None
    location: str | None = Field(None, description="Where players respawn (default: where they died).")
    level: FormulaSrc = Field("level", description="New level. Variables: level, xp, deaths.")
    xp: FormulaSrc = Field("xp", description="New XP towards next level. Variables: level, xp, deaths, xpToNext.")
    currencies: dict[str, FormulaSrc] = Field(
        default_factory=dict,
        description="Per-currency new amount. Variables: amount, cap, level, deaths. Unlisted currencies are kept.")
    inventory: LossDef = Field(default_factory=LossDef, description="Losses of unequipped items.")
    equipped: LossDef = Field(default_factory=LossDef, description="Losses of equipped items.")
    grants: list[GrantDef] = Field(default_factory=list, description="Items/currency handed out on respawn.")

    @field_validator("currencies", mode="before")
    @classmethod
    def _c(cls, v):
        return _none_to_empty(v, {})

    @field_validator("grants", mode="before")
    @classmethod
    def _g(cls, v):
        return _none_to_empty(v, [])


class StartDef(Strict):
    level: int = Field(1, ge=1)
    xp: int = Field(0, ge=0)
    location: str | None = Field(None, description="Starting location (default: first non-safe location).")
    currencies: dict[str, Money] = Field(default_factory=dict)
    items: dict[str, int] = Field(default_factory=dict)
    equipped: list[str] | None = Field(None, description="Items equipped at start (default: best owned gear).")

    @field_validator("currencies", "items", mode="before")
    @classmethod
    def _d(cls, v):
        return _none_to_empty(v, {})


class PolicyDef(Strict):
    """How the simulated player behaves between encounters."""
    location: str = Field("auto", description="'auto' or a fixed location id.")
    max_death_risk: float = Field(
        0.03, gt=0, le=1,
        description="'auto' picks the best location whose per-encounter death chance is at most this.")
    objective: Literal["xp", "income", "balanced"] = Field("xp", description="What 'auto' location choice maximises.")
    sell_loot: bool = Field(True, description="Sell items that are not gear, consumables or wanted crafting inputs.")
    sell_outdated_gear: bool = Field(True, description="Sell gear worse than what is equipped.")
    buy_gear: bool = Field(True, description="Buy the best affordable gear upgrade for each slot.")
    buy_consumables: int = Field(0, ge=0, description="Keep this many consumables in stock (0 = never buy).")
    use_consumables_below: float = Field(0.5, ge=0, le=1, description="Use a consumable when win chance is below this.")
    keep_consumables: int = Field(5, ge=0, description="Sell consumables beyond this many (if sellable).")
    craft: Literal["none", "profitable", "gear", "all"] = Field(
        "profitable", description="none | profitable (outputs sell for more than inputs) | gear (craft upgrades) | all")
    reserve: Money = Field(0, description="Money kept in reserve when shopping.")
    shop_every: int = Field(10, ge=1, description="Encounters between shopping/crafting trips.")


class PlayerDef(Strict):
    start: StartDef = Field(default_factory=StartDef)
    policy: PolicyDef = Field(default_factory=PolicyDef)


class TargetsDef(Strict):
    lifespan: TargetRange = Field(
        [15, 400], description="Expected encounters per life for players inside a location's level_range.")
    even_fight_win: TargetRange = Field([0.5, 0.9], description="Win probability when playerLevel == enemyLevel.")
    encounters_per_level: TargetRange = Field([10, 400], description="Encounters to gain a level at the intended location.")
    hours_to_max_level: TargetRange = Field(
        None, description="Play time (hours, at meta.encounter_rate) from level 1 to max level at the intended locations.")
    max_upkeep_ratio: float | None = Field(0.5, ge=0, description="Max gear upkeep per encounter as a fraction of income.")
    wallet_fill_encounters: float | None = Field(
        50, ge=0, description="Warn if income at the max intended level fills the wallet within this many encounters.")


class IgnoreDef(Strict):
    code: str
    path: str | None = Field(None, description="Only ignore findings whose path starts with this.")
    reason: str | None = None


class LintDef(Strict):
    ignore: list[Union[str, IgnoreDef]] = Field(default_factory=list, description="Rule codes (or {code, path}) to suppress.")

    @field_validator("ignore", mode="before")
    @classmethod
    def _i(cls, v):
        return _none_to_empty(v, [])


class EconomyDef(Strict):
    """Root of an economy file."""
    meta: MetaDef = Field(default_factory=MetaDef)
    params: dict[str, float] = Field(default_factory=dict, description="Global constants usable in every formula.")
    currencies: dict[str, CurrencyDef]
    items: dict[str, ItemDef] = Field(default_factory=dict)
    recipes: dict[str, RecipeDef] = Field(default_factory=dict, description="Crafting, refining, salvage and exchange.")
    enemies: dict[str, EnemyDef]
    locations: dict[str, LocationDef]
    battle: BattleDef
    progression: ProgressionDef
    respawn: RespawnDef = Field(default_factory=RespawnDef)
    player: PlayerDef = Field(default_factory=PlayerDef)
    targets: TargetsDef = Field(default_factory=TargetsDef)
    lint: LintDef = Field(default_factory=LintDef)

    @field_validator("params", "items", "recipes", mode="before")
    @classmethod
    def _maps(cls, v):
        return _none_to_empty(v, {})

    @field_validator("currencies", "enemies", "locations", mode="before")
    @classmethod
    def _maps_req(cls, v):
        return _none_to_empty(v, {})

    @field_validator("currencies", "items", "recipes", "enemies", "locations", mode="before")
    @classmethod
    def _nulls_in_maps(cls, v):
        # `wood:` with no body means "all defaults"
        if isinstance(v, dict):
            return {k: ({} if val is None else val) for k, val in v.items()}
        return v

    @field_validator("meta", "respawn", "player", "targets", "lint", mode="before")
    @classmethod
    def _sections(cls, v):
        return _none_to_empty(v, {})


SECTION_ORDER = [
    "meta", "params", "currencies", "items", "recipes", "enemies", "locations", "battle",
    "progression", "respawn", "player", "targets", "lint",
]
MAP_SECTIONS = {"params", "currencies", "items", "recipes", "enemies", "locations"}
REQUIRED_SECTIONS = ["currencies", "enemies", "locations", "battle", "progression"]


def all_field_names() -> set[str]:
    names: set[str] = set()
    for model in (MetaDef, DenominationDef, CurrencyDef, ItemDef, RecipeDef, DropDef, EnemyDef,
                  EncounterDef, LocationDef, BattleDef, ProgressionDef, LossDef, GrantDef,
                  RespawnDef, StartDef, PolicyDef, PlayerDef, TargetsDef, IgnoreDef, LintDef,
                  EconomyDef):
        names.update(model.model_fields)
    return names


def json_schema() -> dict:
    schema = EconomyDef.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Game economy"
    return schema
