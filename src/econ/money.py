"""Currencies, denominations and money amounts.

Every currency has a *base unit* (its smallest denomination, value 1). Internally all amounts
are integers in base units. In economy files, amounts can be written as:

* a number                 ``150``          -> 150 base units of the default currency
* a denominated string     ``"1g 50s 3c"``  -> uses denomination symbols or names
* a currency-suffixed str  ``"5 gem"``      -> 5 base units of currency ``gem``
* a mapping                ``{gem: 5, coin: "2s"}`` for multi-currency prices
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

Bundle = dict[str, int]  # currency id -> base units

_TOKEN = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*([A-Za-z_]+)?\s*,?")


class MoneyError(ValueError):
    pass


@dataclass(frozen=True)
class Denomination:
    name: str
    symbol: str
    value: int


@dataclass
class Currency:
    id: str
    denominations: list[Denomination] = field(default_factory=list)  # ascending by value
    cap: int | None = None
    primary: bool = False
    description: str = ""

    def format(self, amount: int | float) -> str:
        """Human-readable amount, e.g. ``1g 50s 3c`` or ``15 gem``."""
        if isinstance(amount, float) and not amount.is_integer():
            return self._format_fractional(amount)
        amount = int(amount)
        if not self.denominations:
            return f"{amount} {self.id}"
        sign = "-" if amount < 0 else ""
        rest = abs(amount)
        parts: list[str] = []
        for d in sorted(self.denominations, key=lambda d: -d.value):
            if rest >= d.value:
                n, rest = divmod(rest, d.value)
                parts.append(f"{n}{d.symbol}")
        if not parts:
            smallest = min(self.denominations, key=lambda d: d.value)
            return f"0{smallest.symbol}"
        return sign + " ".join(parts)

    def _format_fractional(self, amount: float) -> str:
        if not self.denominations:
            return f"{amount:.2f} {self.id}"
        # pick the largest denomination that keeps the number readable
        for d in sorted(self.denominations, key=lambda d: -d.value):
            if abs(amount) >= d.value:
                return f"{amount / d.value:.2f}{d.symbol}"
        smallest = min(self.denominations, key=lambda d: d.value)
        return f"{amount / smallest.value:.2f}{smallest.symbol}"


class MoneyParser:
    """Parses money expressions given the economy's currencies."""

    def __init__(self, currencies: dict[str, Currency], default: str | None):
        self.currencies = currencies
        self.default = default
        # unit name -> (currency id, multiplier)
        self.units: dict[str, tuple[str, int]] = {}
        for cid, cur in currencies.items():
            self.units.setdefault(cid, (cid, 1))
        for cid, cur in currencies.items():
            for d in cur.denominations:
                self.units[d.symbol] = (cid, d.value)
                self.units[d.name] = (cid, d.value)

    def parse(self, value, default: str | None = None, allow_negative: bool = False) -> Bundle:
        default = default or self.default
        if value is None:
            return {}
        if isinstance(value, bool):
            raise MoneyError(f"expected an amount of money, got {value!r}")
        if isinstance(value, dict):
            out: Bundle = {}
            for k, v in value.items():
                if k not in self.currencies:
                    raise MoneyError(
                        f"unknown currency {k!r} (known: {', '.join(self.currencies) or 'none'})"
                    )
                for cid, amt in self.parse(v, default=k, allow_negative=allow_negative).items():
                    out[cid] = out.get(cid, 0) + amt
            return out
        if isinstance(value, (int, float)):
            if default is None:
                raise MoneyError("no currency defined for a bare number")
            return {default: self._to_int(value, 1, str(value), allow_negative)}
        if isinstance(value, str):
            return self._parse_str(value, default, allow_negative)
        raise MoneyError(f"expected an amount of money, got {type(value).__name__}")

    def _to_int(self, number: float, mult: int, src: str, allow_negative: bool) -> int:
        total = number * mult
        if abs(total - round(total)) > 1e-9:
            raise MoneyError(f"{src!r} is not a whole number of base units")
        total = int(round(total))
        if total < 0 and not allow_negative:
            raise MoneyError(f"{src!r} is negative")
        return total

    def _parse_str(self, text: str, default: str | None, allow_negative: bool) -> Bundle:
        s = text.strip()
        if not s:
            raise MoneyError("empty amount")
        out: Bundle = {}
        pos = 0
        while pos < len(s):
            m = _TOKEN.match(s, pos)
            if not m or m.end() == pos:
                raise MoneyError(f"cannot parse amount {text!r} near {s[pos:]!r}")
            num_s, unit = m.group(1), m.group(2)
            pos = m.end()
            if s[pos:pos + 1] == "+":
                pos += 1
            number = float(num_s)
            if unit is None:
                if default is None:
                    raise MoneyError(f"amount {text!r} needs a unit")
                cid, mult = default, 1
            else:
                if unit not in self.units:
                    known = sorted(self.units)
                    raise MoneyError(f"unknown money unit {unit!r} in {text!r} (known: {', '.join(known)})")
                cid, mult = self.units[unit]
            out[cid] = out.get(cid, 0) + self._to_int(number, mult, text, allow_negative)
        return out

    def format(self, bundle: Bundle | None, empty: str = "free") -> str:
        if not bundle:
            return empty
        parts = []
        for cid, amt in bundle.items():
            if cid in self.currencies:
                parts.append(self.currencies[cid].format(amt))
            else:
                parts.append(f"{amt} {cid}")
        return " + ".join(parts)


def bundle_add(a: Bundle, b: Bundle, scale: int | float = 1) -> Bundle:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v * scale
    return {k: v for k, v in out.items() if v != 0}
