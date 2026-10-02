"""Load economy YAML (a single file or a directory of files) and validate its shape."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError

from .lint.findings import Finding, fmt_path
from .model import MAP_SECTIONS, REQUIRED_SECTIONS, SECTION_ORDER, EconomyDef, all_field_names


class SourceMap:
    """Maps paths like ('items', 'sword', 'buy') to (file, line)."""

    def __init__(self):
        self.lines: dict[tuple, tuple[str, int]] = {}

    def add(self, path: tuple, file: str, line: int):
        self.lines.setdefault(path, (file, line))

    def locate(self, path) -> tuple[str | None, int | None]:
        if isinstance(path, str):
            path = parse_path(path)
        path = tuple(path)
        while path:
            if path in self.lines:
                return self.lines[path]
            path = path[:-1]
        return (None, None)

    def merge(self, other: "SourceMap"):
        for k, v in other.lines.items():
            self.lines.setdefault(k, v)


def parse_path(s: str) -> tuple:
    """'locations.meadow.enemies[1].weight' -> ('locations', 'meadow', 'enemies', 1, 'weight')"""
    out: list = []
    for part in s.split("."):
        while "[" in part:
            head, rest = part.split("[", 1)
            if head:
                out.append(head)
            idx, part = rest.split("]", 1)
            out.append(int(idx))
        if part:
            out.append(part)
    return tuple(out)


@dataclass
class LoadResult:
    data: dict[str, Any] | None
    source: SourceMap
    files: list[str]
    findings: list[Finding] = field(default_factory=list)
    model: EconomyDef | None = None

    @property
    def ok(self) -> bool:
        return self.model is not None and not any(f.severity == "error" for f in self.findings)


def _plain(node):
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def _walk(node, path: tuple, file: str, smap: SourceMap):
    if isinstance(node, CommentedMap):
        for k in node:
            try:
                line = node.lc.key(k)[0] + 1
            except Exception:  # pragma: no cover - defensive
                line = None
            if line:
                smap.add(path + (str(k),), file, line)
            _walk(node[k], path + (str(k),), file, smap)
    elif isinstance(node, CommentedSeq):
        for i, v in enumerate(node):
            try:
                line = node.lc.item(i)[0] + 1
            except Exception:  # pragma: no cover
                line = None
            if line:
                smap.add(path + (i,), file, line)
            _walk(v, path + (i,), file, smap)


def load_yaml_file(path: Path) -> tuple[dict | None, SourceMap, list[Finding]]:
    smap = SourceMap()
    yaml = YAML(typ="rt")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.load(fh)
    except MarkedYAMLError as e:
        line = e.problem_mark.line + 1 if e.problem_mark else None
        msg = (e.problem or str(e)).strip()
        return None, smap, [Finding("yaml", "error", f"YAML syntax error: {msg}", file=str(path), line=line)]
    except OSError as e:
        return None, smap, [Finding("yaml", "error", f"cannot read file: {e}", file=str(path))]
    if raw is None:
        return {}, smap, []
    if not isinstance(raw, dict):
        return None, smap, [Finding("yaml", "error", "top level of an economy file must be a mapping", file=str(path), line=1)]
    _walk(raw, (), str(path), smap)
    return _plain(raw), smap, []


def load_path(path: str | Path) -> LoadResult:
    """Load a file or a directory (all *.yaml / *.yml files, recursively, merged)."""
    p = Path(path)
    if p.is_dir():
        files = sorted(
            f for f in p.rglob("*") if f.suffix in (".yaml", ".yml") and not any(part.startswith(".") for part in f.parts)
        )
        if not files:
            return LoadResult(None, SourceMap(), [], [Finding("yaml", "error", f"no .yaml files in {p}")])
    elif p.exists():
        files = [p]
    else:
        return LoadResult(None, SourceMap(), [], [Finding("yaml", "error", f"no such file: {p}")])

    merged: dict[str, Any] = {}
    origin: dict[tuple, str] = {}
    smap = SourceMap()
    findings: list[Finding] = []
    for f in files:
        data, fmap, errs = load_yaml_file(f)
        findings.extend(errs)
        if data is None:
            continue
        smap.merge(fmap)
        for section, value in data.items():
            if section in MAP_SECTIONS and isinstance(value, dict):
                target = merged.setdefault(section, {})
                if not isinstance(target, dict):
                    continue
                for key, v in value.items():
                    if key in target:
                        file, line = fmap.locate((section, key))
                        findings.append(Finding(
                            "duplicate-definition", "error",
                            f"{section}.{key} is defined more than once (also in {origin.get((section, key))})",
                            path=f"{section}.{key}", file=file, line=line))
                        continue
                    target[key] = v
                    origin[(section, key)] = str(f)
            else:
                if section in merged:
                    file, line = fmap.locate((section,))
                    findings.append(Finding(
                        "duplicate-definition", "error",
                        f"section '{section}' appears more than once (also in {origin.get((section,))})",
                        path=section, file=file, line=line))
                    continue
                merged[section] = value
                origin[(section,)] = str(f)
    result = LoadResult(merged, smap, [str(f) for f in files], findings)
    if any(x.severity == "error" for x in findings):
        return result
    _validate(result)
    return result


_FIELD_NAMES = all_field_names()


def _data_prefix(data, loc: tuple) -> tuple:
    """Longest prefix of `loc` that exists in `data` (drops pydantic union tags)."""
    cur = data
    out: list = []
    for part in loc:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and isinstance(part, int) and 0 <= part < len(cur):
            cur = cur[part]
        else:
            # keep a missing *field name* (for "missing"/"extra" errors) but nothing past it
            if isinstance(part, str) and isinstance(cur, dict):
                out.append(part)
            break
        out.append(part)
    return tuple(out)


def _validate(result: LoadResult) -> None:
    data = result.data or {}
    for section in data:
        if section not in SECTION_ORDER:
            close = difflib.get_close_matches(section, SECTION_ORDER, n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            file, line = result.source.locate((section,))
            result.findings.append(Finding(
                "schema", "error", f"unknown top-level section '{section}'{hint}",
                path=section, file=file, line=line,
                fix=f"Valid sections: {', '.join(SECTION_ORDER)}"))
    missing = [s for s in REQUIRED_SECTIONS if s not in data]
    for s in missing:
        result.findings.append(Finding(
            "missing-section", "error", f"required section '{s}' is missing", path=s,
            fix="Run `econ new` to see a complete template."))
    if result.findings:
        return
    try:
        result.model = EconomyDef.model_validate(data)
    except ValidationError as e:
        grouped: dict[tuple, list[dict]] = {}
        for err in e.errors():
            key = _data_prefix(data, tuple(err["loc"]))
            grouped.setdefault(key, []).append(err)
        for loc, errs in grouped.items():
            path = fmt_path(loc)
            file, line = result.source.locate(loc)
            types = {x["type"] for x in errs}
            if "extra_forbidden" in types:
                name = str(loc[-1]) if loc else "?"
                close = difflib.get_close_matches(name, sorted(_FIELD_NAMES), n=1, cutoff=0.6)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                msg = f"unknown field '{name}'{hint}"
            elif "missing" in types:
                msg = f"missing required field '{loc[-1]}'" if loc else "missing required field"
            else:
                msgs = sorted({x["msg"] for x in errs})
                got = errs[0].get("input")
                got_s = repr(got)
                if len(got_s) > 60:
                    got_s = got_s[:57] + "..."
                msg = f"invalid value {got_s}: " + "; ".join(msgs[:3])
            result.findings.append(Finding("schema", "error", msg, path=path, file=file, line=line))
