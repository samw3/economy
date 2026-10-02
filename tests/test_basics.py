"""Money, formulas, loading and compiling."""

import math

import pytest

from econ.economy import load_economy
from econ.formula import Formula, FormulaError
from econ.money import Currency, Denomination, MoneyError, MoneyParser


@pytest.fixture
def parser():
    coin = Currency("coin", [Denomination("copper", "c", 1), Denomination("silver", "s", 100),
                             Denomination("gold", "g", 10000)])
    return MoneyParser({"coin": coin, "gem": Currency("gem")}, "coin")


@pytest.mark.parametrize("value,expected", [
    (150, {"coin": 150}),
    ("1g 50s 3c", {"coin": 15003}),
    ("1g50s", {"coin": 15000}),
    ("1.5g", {"coin": 15000}),
    ("2 silver", {"coin": 200}),
    ("5 gem", {"gem": 5}),
    ({"gem": 5, "coin": "2s"}, {"gem": 5, "coin": 200}),
    ("0c", {"coin": 0}),
])
def test_money_parse(parser, value, expected):
    assert parser.parse(value) == expected


@pytest.mark.parametrize("bad", ["1x", "0.5c", "-3c", "abc", True, {"nope": 1}])
def test_money_errors(parser, bad):
    with pytest.raises(MoneyError):
        parser.parse(bad)


def test_money_format(parser):
    coin = parser.currencies["coin"]
    assert coin.format(15003) == "1g 50s 3c"
    assert coin.format(0) == "0c"
    assert coin.format(-250) == "-2s 50c"
    assert parser.currencies["gem"].format(3) == "3 gem"
    assert parser.format({"coin": 200, "gem": 1}) == "2s + 1 gem"


def test_formula_eval_and_names():
    f = Formula.compile("clamp(sigmoid((delta + bias) / 2), 0.1, 0.9)", {"delta"}, {"bias": 1})
    assert f.names == {"delta", "bias"}
    assert f(delta=0) == pytest.approx(1 / (1 + math.exp(-0.5)))
    assert f(delta=100) == 0.9
    g = Formula.compile("floor(25 * level ^ 1.5)", {"level"})
    assert g(level=4) == 200


@pytest.mark.parametrize("src,msg", [
    ("__import__('os')", "unknown function"),
    ("level.real", "not allowed"),
    ("lvl * 2", "did you mean 'level'"),
    ("foo(level)", "unknown function"),
    ("[1, 2]", "not allowed"),
    ("lambda: 1", "not allowed"),
])
def test_formula_rejects(src, msg):
    with pytest.raises(FormulaError, match=msg):
        Formula.compile(src, {"level"})


def test_formula_runtime_errors():
    with pytest.raises(FormulaError, match="division by zero"):
        Formula.compile("1 / level", {"level"})(level=0)
    with pytest.raises(FormulaError):
        Formula.compile("9 ** 9 ** 9", {"level"})(level=1)  # floats: overflow, not a hang
    with pytest.raises(FormulaError, match="complex"):
        Formula.compile("(-8) ** (1/3)", {"level"})(level=1)


def test_schema_error_has_line_and_suggestion(write_yaml, base_yaml):
    path = write_yaml(base_yaml.replace("half_life: 100", "hal_life: 100"))
    eco, findings, _ = load_economy(path)
    assert eco is None
    f = next(x for x in findings if x.code == "schema")
    assert "half_life" in f.message and f.line == 9 and f.path == "items.sword.hal_life"


def test_unknown_reference_suggestion(write_yaml, base_yaml):
    path = write_yaml(base_yaml.replace("{item: gel, chance: 0.5}", "{item: gell, chance: 0.5}"))
    eco, findings, _ = load_economy(path)
    f = next(x for x in findings if x.code == "unknown-reference")
    assert "did you mean 'gel'" in f.message and f.path == "enemies.slime.drops[1].item"


def test_bad_formula_reported(write_yaml, base_yaml):
    path = write_yaml(base_yaml.replace("(delta + 2) / 1.2", "(delta + bias) / 1.2"))
    eco, findings, _ = load_economy(path)
    assert any(x.code == "bad-formula" and "bias" in x.message for x in findings)


def test_compile_ok(write_yaml, base_yaml):
    eco, findings, _ = load_economy(write_yaml(base_yaml))
    assert eco is not None, findings
    assert eco.primary == "coin"
    assert eco.currencies["coin"].cap == 10000
    assert eco.items["sword"].buy == {"coin": 50}
    assert eco.xp_table[1] == 30
    meadow = eco.locations["meadow"]
    assert sum(v.prob for v in meadow.variants) == pytest.approx(1.0)
    assert eco.start.location == "meadow"  # first combat location by level


def test_half_life_break_chance(write_yaml, base_yaml):
    eco, _, _ = load_economy(write_yaml(base_yaml))
    sword = eco.items["sword"]
    # probability of surviving `half_life` uses is exactly 1/2
    assert (1 - sword.break_chance) ** sword.half_life == pytest.approx(0.5)


def test_directory_merge_and_duplicates(tmp_path, base_yaml):
    from ruamel.yaml import YAML
    yaml = YAML()
    import io
    data = yaml.load(io.StringIO(base_yaml))
    d = tmp_path / "eco"
    d.mkdir()
    parts = {"currencies.yaml": ["currencies"], "items.yaml": ["items"],
             "world.yaml": ["enemies", "locations"], "rules.yaml": ["battle", "progression"]}
    for name, keys in parts.items():
        with open(d / name, "w") as fh:
            yaml.dump({k: data[k] for k in keys}, fh)
    eco, findings, res = load_economy(str(d))
    assert eco is not None, findings
    assert len(res.files) == 4
    # duplicate id across files
    with open(d / "more_items.yaml", "w") as fh:
        fh.write("items:\n  gel: {sell: 3c}\n")
    eco, findings, _ = load_economy(str(d))
    assert eco is None
    assert any(f.code == "duplicate-definition" and "items.gel" in f.message for f in findings)
