"""Safe arithmetic formulas.

Formulas in economy files are small Python-like expressions, e.g.::

    clamp(sigmoid((playerLevel - enemyLevel + bias) / spread), 0.02, 0.98)
    floor(25 * level ^ 1.6)
    floor(amount * 0.9)

Supported: numbers, variables, ``+ - * / // % **`` (``^`` is accepted as power), unary minus,
comparisons, ``and``/``or``/``not``, ``a if cond else b`` and the functions in ``FUNCTIONS``.
Everything else (attribute access, indexing, lambdas, ...) is rejected before evaluation.
"""

from __future__ import annotations

import ast
import difflib
import math
from dataclasses import dataclass, field
from types import CodeType


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def lerp(a, b, t):
    return a + (b - a) * t


def smoothstep(edge0, edge1, x):
    t = clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


FUNCTIONS = {
    "min": min,
    "max": max,
    "abs": abs,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "pow": math.pow,
    "clamp": clamp,
    "sigmoid": sigmoid,
    "lerp": lerp,
    "smoothstep": smoothstep,
}

FUNCTION_HELP = {
    "min(a, b, ...)": "smallest argument",
    "max(a, b, ...)": "largest argument",
    "abs(x)": "absolute value",
    "floor(x) / ceil(x) / round(x)": "rounding to integers",
    "sqrt(x), exp(x), log(x), log2(x), log10(x), pow(x, y)": "usual math functions",
    "clamp(x, lo, hi)": "x limited to [lo, hi]",
    "sigmoid(x)": "1 / (1 + e^-x), an S-curve from 0 to 1",
    "lerp(a, b, t)": "a + (b - a) * t",
    "smoothstep(e0, e1, x)": "smooth 0→1 ramp between e0 and e1",
}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
)


class FormulaError(ValueError):
    pass


class _FloatLiterals(ast.NodeTransformer):
    """Turn integer literals into floats so `**` can never build gigantic integers."""

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, bool):
            return node
        if isinstance(node.value, int):
            return ast.copy_location(ast.Constant(float(node.value)), node)
        return node


@dataclass
class Formula:
    source: str
    names: frozenset[str]
    code: CodeType
    params: dict[str, float] = field(default_factory=dict)

    @classmethod
    def compile(cls, source, allowed: set[str] | frozenset[str], params: dict[str, float] | None = None,
                what: str = "formula") -> "Formula":
        params = dict(params or {})
        if isinstance(source, bool):
            raise FormulaError(f"{what}: expected a formula, got {source!r}")
        if isinstance(source, (int, float)):
            source = repr(source)
        if not isinstance(source, str) or not source.strip():
            raise FormulaError(f"{what}: expected a formula string")
        text = source.replace("^", "**")
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as e:
            hint = ""
            if source.count("(") != source.count(")"):
                hint = (" — if this formula sits inside a YAML {…} mapping, a comma ends it early: "
                        "quote it, e.g. coin: \"max(amount, 50)\"")
            raise FormulaError(f"{what}: syntax error in {source!r}: {e.msg}{hint}") from None
        func_nodes: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise FormulaError(f"{what}: {type(node).__name__} is not allowed in formulas ({source!r})")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                    fname = getattr(node.func, "id", "?")
                    hint = _suggest(fname, FUNCTIONS)
                    raise FormulaError(f"{what}: unknown function {fname!r}{hint} in {source!r}")
                if node.keywords:
                    raise FormulaError(f"{what}: keyword arguments are not supported ({source!r})")
                func_nodes.add(id(node.func))
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
                raise FormulaError(f"{what}: only numeric constants are allowed ({source!r})")
        free = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in func_nodes}
        unknown = sorted(n for n in free if n not in allowed and n not in params)
        if unknown:
            pool = set(allowed) | set(params)
            msgs = [f"{n!r}{_suggest(n, pool)}" for n in unknown]
            avail = ", ".join(sorted(pool)) or "none"
            raise FormulaError(f"{what}: unknown variable {', '.join(msgs)} in {source!r} (available: {avail})")
        tree = ast.fix_missing_locations(_FloatLiterals().visit(tree))
        code = compile(tree, f"<{what}>", "eval")
        return cls(source=str(source), names=frozenset(free), code=code, params=params)

    def __call__(self, **env) -> float:
        return self.eval(env)

    def eval(self, env: dict) -> float:
        scope = dict(self.params)
        scope.update(env)
        try:
            value = eval(self.code, {"__builtins__": {}, **FUNCTIONS}, scope)  # noqa: S307 - AST-validated
        except ZeroDivisionError:
            raise FormulaError(f"division by zero evaluating {self.source!r} with {_fmt_env(env)}") from None
        except (ValueError, OverflowError) as e:
            raise FormulaError(f"{e} evaluating {self.source!r} with {_fmt_env(env)}") from None
        except TypeError as e:
            raise FormulaError(f"{e} evaluating {self.source!r}") from None
        if isinstance(value, bool):
            value = float(value)
        if isinstance(value, complex):
            raise FormulaError(f"complex result evaluating {self.source!r} with {_fmt_env(env)}")
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            raise FormulaError(f"non-finite result ({value}) evaluating {self.source!r} with {_fmt_env(env)}")
        return value


def _fmt_env(env: dict) -> str:
    return ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in env.items())


def _suggest(name: str, pool) -> str:
    close = difflib.get_close_matches(name, list(pool), n=1, cutoff=0.6)
    return f" (did you mean {close[0]!r}?)" if close else ""
