"""Run Lean on a generated module and map the results back to theorems."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .generate import LeanModule

_ERR = re.compile(r"^(?P<file>[^\n:]+\.lean):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning|info)(?:\([^)]*\))?: ?(?P<msg>.*)$")


def lean_dir() -> Path:
    env = os.environ.get("ECON_LEAN_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "lean" / "lakefile.toml"
        if cand.exists():
            return cand.parent
    return here.parents[3] / "lean"


def find_lake() -> str | None:
    lake = shutil.which("lake")
    if lake:
        return lake
    cand = Path.home() / ".elan" / "bin" / "lake"
    return str(cand) if cand.exists() else None


@dataclass
class LeanResult:
    available: bool
    ok: bool = False
    path: str = ""
    seconds: float = 0.0
    statuses: dict[str, str] = field(default_factory=dict)   # theorem -> "proved" | "failed: ..."
    errors: list[str] = field(default_factory=list)
    axioms: dict[str, list[str]] = field(default_factory=dict)
    cached: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available, "ok": self.ok, "file": self.path, "seconds": round(self.seconds, 2),
                "cached": self.cached, "theorems": self.statuses, "axioms": self.axioms,
                "errors": self.errors[:20], "message": self.message}


def _library_hash(ld: Path) -> str:
    h = hashlib.sha256()
    for p in sorted([ld / "EconomyModel.lean", *(ld / "EconomyModel").glob("*.lean"), ld / "lean-toolchain"]):
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def ensure_built(lake: str, ld: Path, timeout: int = 900) -> tuple[bool, str]:
    olean = ld / ".lake" / "build" / "lib" / "lean" / "EconomyModel.olean"
    srcs = [ld / "EconomyModel.lean", *(ld / "EconomyModel").glob("*.lean")]
    if olean.exists() and all(s.stat().st_mtime <= olean.stat().st_mtime for s in srcs):
        return True, ""
    try:
        proc = subprocess.run([lake, "build", "EconomyModel"], cwd=ld, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"lake build failed: {e}"
    if proc.returncode != 0:
        return False, "lake build EconomyModel failed:\n" + (proc.stdout + proc.stderr)[-3000:]
    return True, ""


def check(module: LeanModule, timeout: int = 900, use_cache: bool = True) -> LeanResult:
    lake = find_lake()
    ld = lean_dir()
    if lake is None:
        return LeanResult(False, message="Lean toolchain not found (install with `brew install elan-init && elan default stable`)")
    if not (ld / "lakefile.toml").exists():
        return LeanResult(False, message=f"Lean project not found at {ld} (set ECON_LEAN_DIR to the repo's lean/ directory)")
    gen_dir = ld / "Generated"
    gen_dir.mkdir(exist_ok=True)
    path = gen_dir / f"{module.name}.lean"
    path.write_text(module.text, encoding="utf-8")
    key = hashlib.sha256((module.text + _library_hash(ld)).encode()).hexdigest()[:24]
    cache_dir = ld / ".lake" / "econ-cache"
    cache_file = cache_dir / f"{key}.json"
    rel = os.path.relpath(path, Path.cwd()) if str(path).startswith(str(Path.cwd())) else str(path)
    if use_cache and cache_file.exists():
        data = json.loads(cache_file.read_text())
        return LeanResult(True, data["ok"], rel, data["seconds"], data["statuses"], data["errors"], data.get("axioms", {}),
                          cached=True, message=data.get("message", ""))
    ok, msg = ensure_built(lake, ld)
    if not ok:
        return LeanResult(False, path=rel, message=msg)
    t0 = time.time()
    try:
        proc = subprocess.run([lake, "env", "lean", str(path.relative_to(ld))], cwd=ld, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return LeanResult(True, False, rel, time.time() - t0, {t.name: "failed: timeout" for t in module.theorems},
                          ["timeout"], message=f"Lean timed out after {timeout}s")
    secs = time.time() - t0
    out = proc.stdout + "\n" + proc.stderr
    errors: list[tuple[int, str]] = []
    infos: list[tuple[int, str]] = []
    cur = None
    for line in out.splitlines():
        m = _ERR.match(line)
        if m:
            entry = [int(m["line"]), m["msg"]]
            if m["sev"] == "error":
                errors.append(entry)
            elif m["sev"] == "info":
                infos.append(entry)
            cur = entry
        elif cur is not None and line.strip():
            cur[1] += "\n" + line
    statuses: dict[str, str] = {}
    unmatched: list[str] = []
    for t in module.theorems:
        statuses[t.name] = "proved"
    for ln, msg in errors:
        hit = None
        for t in module.theorems:
            if t.start <= ln <= max(t.end, t.start):
                hit = t
                break
        if hit is not None:
            statuses[hit.name] = "failed: " + msg.splitlines()[0][:300]
        else:
            unmatched.append(f"line {ln}: {msg.splitlines()[0][:300]}")
    if unmatched or proc.returncode != 0 and not errors:
        # an error outside a theorem (e.g. in a definition) invalidates everything after it
        for name in statuses:
            if statuses[name] == "proved" and unmatched:
                statuses[name] = "unverified (module error)"
    axioms: dict[str, list[str]] = {}
    for m in re.finditer(r"'([^'\n]+)' depends on axioms: \[([^\]]*)\]", out):
        axioms[m.group(1).split(".")[-1]] = [a.strip() for a in m.group(2).split(",") if a.strip()]
    for m in re.finditer(r"'([^'\n]+)' does not depend on any axioms", out):
        axioms[m.group(1).split(".")[-1]] = []
    all_ok = all(v == "proved" for v in statuses.values()) and not unmatched
    res = LeanResult(True, all_ok, rel, secs, statuses, unmatched + [f"line {l}: {m.splitlines()[0]}" for l, m in errors],
                     axioms, message="" if all_ok else "some claims failed to verify")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ok": res.ok, "seconds": res.seconds, "statuses": res.statuses,
                                          "errors": res.errors, "axioms": res.axioms, "message": res.message}))
    except OSError:
        pass
    return res
