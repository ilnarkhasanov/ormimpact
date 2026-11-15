"""Run mypy or pyright before and after a model change and keep the new errors (layer: checker)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .core import LAYER_CHECKER, Change, Finding

MYPY_LINE = re.compile(
    r"^(?P<path>[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?(?:\d+:\d+:)? (?P<sev>error|warning|note): "
    r"(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)


@dataclass(frozen=True)
class Diag:
    path: str
    line: int
    col: int
    code: str
    message: str


@dataclass
class CheckerRun:
    tool: str
    cwd: str
    command: list[str]
    returncode: int
    diags: list[Diag]
    stderr: str = ""
    seconds: float = 0.0


def has_config(root: Path, tool: str) -> bool:
    if tool == "mypy":
        if (root / "mypy.ini").exists() or (root / ".mypy.ini").exists():
            return True
        for name, marker in (("setup.cfg", "[mypy"), ("pyproject.toml", "[tool.mypy")):
            p = root / name
            if p.exists() and marker in p.read_text(encoding="utf-8", errors="replace"):
                return True
        return False
    if tool == "pyright":
        if (root / "pyrightconfig.json").exists():
            return True
        p = root / "pyproject.toml"
        return p.exists() and "[tool.pyright" in p.read_text(encoding="utf-8", errors="replace")
    return False


def available(tool: str) -> bool:
    if tool == "mypy":
        try:
            import mypy  # noqa: F401

            return True
        except ImportError:
            return shutil.which("mypy") is not None
    if tool == "pyright":
        return shutil.which("pyright") is not None
    return False


def auto_tool(root: Path) -> str | None:
    for tool in ("mypy", "pyright"):
        if has_config(root, tool) and available(tool):
            return tool
    return None


def _base_command(tool: str, cache_dir: str) -> list[str]:
    if tool == "mypy":
        try:
            import mypy  # noqa: F401

            base = [sys.executable, "-m", "mypy"]
        except ImportError:
            base = ["mypy"]
        return base + ["--no-error-summary", "--show-error-codes", "--no-color-output", "--hide-error-context",
                       "--show-column-numbers", "--no-pretty", "--cache-dir", cache_dir]
    if tool == "pyright":
        return ["pyright", "--outputjson"]
    raise ValueError(f"unknown checker {tool!r}")


def run_checker(tool: str, cwd: Path, args: list[str] | None = None, targets: list[str] | None = None,
                command: str | None = None, timeout: int = 3600) -> CheckerRun:
    import time

    with tempfile.TemporaryDirectory(prefix="ormimpact-cache-") as cache:
        if command:
            cmd = shlex.split(command)
        else:
            cmd = _base_command(tool, cache) + list(args or [])
            cmd += list(targets) if targets else ["."]
        env = dict(os.environ)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        t0 = time.time()
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace")
        dt = time.time() - t0
    diags = parse_output(tool, proc.stdout, cwd)
    return CheckerRun(tool, str(cwd), cmd, proc.returncode, diags, proc.stderr[-4000:], dt)


def _norm(path: str, cwd: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(cwd.resolve())
        except ValueError:
            return p.as_posix()
    return Path(os.path.normpath(str(p))).as_posix()


def parse_output(tool: str, out: str, cwd: Path) -> list[Diag]:
    diags: list[Diag] = []
    if tool == "pyright":
        try:
            data = json.loads(out[out.find("{"):]) if "{" in out else {}
        except ValueError:
            data = {}
        for d in data.get("generalDiagnostics", []):
            if d.get("severity") != "error":
                continue
            rng = d.get("range", {}).get("start", {})
            diags.append(Diag(_norm(d.get("file", ""), cwd), int(rng.get("line", 0)) + 1,
                              int(rng.get("character", 0)) + 1, d.get("rule") or "error",
                              " ".join(str(d.get("message", "")).split())))
        return diags
    for line in out.splitlines():
        m = MYPY_LINE.match(line.strip())
        if not m or m.group("sev") != "error":
            continue
        diags.append(Diag(_norm(m.group("path"), cwd), int(m.group("line")), int(m.group("col") or 0),
                          m.group("code") or "error", m.group("msg").strip()))
    return diags


def new_errors(before: list[Diag], after: list[Diag], edited: set[str]) -> list[Diag]:
    """Errors present after the change but not before (multiset difference).

    Line numbers are only compared in files that were not edited.
    """
    def key(d: Diag):
        if d.path in edited:
            return (d.path, None, d.code, d.message)
        return (d.path, d.line, d.code, d.message)

    remaining = Counter(key(d) for d in before)
    out = []
    for d in after:
        k = key(d)
        if remaining[k] > 0:
            remaining[k] -= 1
        else:
            out.append(d)
    return out


def findings_from(diags: list[Diag], tool: str, changes: list[Change], lines_of) -> list[Finding]:
    label = "; ".join(c.describe() for c in changes) if len(changes) <= 3 else f"{len(changes)} changes"
    out = []
    for d in diags:
        out.append(Finding(path=d.path, line=d.line, col=d.col, layer=LAYER_CHECKER, kind=f"{tool}:{d.code}",
                           confidence="high", change=_guess_change(d, changes) or label,
                           evidence=d.message[:200], symbol=None, detail=lines_of(d.path, d.line)))
    return out


def _guess_change(d: Diag, changes: list[Change]) -> str | None:
    hits = [c for c in changes if re.search(rf"\b{re.escape(c.field)}\b", d.message)]
    if len(hits) == 1:
        return hits[0].describe()
    return None


def copy_tree(src: Path, dest: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                                    ".pytest_cache", ".tox", ".ruff_cache")
    shutil.copytree(src, dest, ignore=ignore, symlinks=True, dirs_exist_ok=True)
