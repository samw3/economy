import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def write_yaml(tmp_path):
    """Write a YAML snippet to a temp file and return its path."""
    def _write(text: str, name: str = "eco.yaml") -> str:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        return str(p)
    return _write


BASE = """
currencies:
  coin:
    denominations:
      - {name: copper, symbol: c, value: 1}
      - {name: silver, symbol: s, value: 100}
    wallet_cap: 100s
items:
  sword: {buy: 50c, sell: 10c, slot: weapon, power: 1, half_life: 100}
  gel: {sell: 2c}
enemies:
  slime: {level: 1, xp: 5, drops: [{currency: coin, amount: [1, 3]}, {item: gel, chance: 0.5}]}
  wolf: {level: 3, xp: 10, drops: [{currency: coin, amount: [3, 6]}]}
locations:
  town: {safe: true}
  meadow:
    level_range: [1, 3]
    enemies: [{enemy: slime, weight: 3}, {enemy: wolf, weight: 1}]
battle:
  formula: clamp(sigmoid((delta + 2) / 1.2), 0.01, 0.99)
progression:
  max_level: 5
  xp_to_next: floor(30 * level ^ 1.5)
"""


@pytest.fixture
def base_yaml():
    return BASE
