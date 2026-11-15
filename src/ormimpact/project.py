"""File discovery and cached parsing of a project tree."""

from __future__ import annotations

import ast
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", ".env",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist", "site-packages", ".idea", ".vscode", "htmlcov", ".eggs",
    "static", "staticfiles", "media",
}

TEMPLATE_SUFFIXES = {".html", ".htm", ".jinja", ".jinja2", ".j2", ".djhtml", ".txt", ".xml"}
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".csv"}
SQL_SUFFIXES = {".sql"}


def is_migration(rel: str) -> bool:
    parts = rel.split("/")
    if "migrations" in parts:
        return True
    if "versions" in parts and any(p in ("alembic", "migrations", "migration") for p in parts):
        return True
    if "alembic" in parts and "versions" in parts:
        return True
    return False


@dataclass
class ParsedModule:
    path: Path
    rel: str
    source: str
    tree: ast.Module
    lines: list[str] = field(default_factory=list)
    _symbols: list[tuple[int, int, str]] | None = None
    _starts: list[int] | None = None
    imports: set[str] = field(default_factory=set)

    def line(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def snippet(self, lineno: int, limit: int = 160) -> str:
        s = self.line(lineno).strip()
        return s if len(s) <= limit else s[: limit - 1] + "…"

    def segment(self, node: ast.AST) -> str:
        seg = ast.get_source_segment(self.source, node)
        return seg if seg is not None else ast.unparse(node)

    def symbol_at(self, lineno: int) -> str | None:
        """Innermost function or class enclosing ``lineno``."""
        if self._symbols is None:
            syms: list[tuple[int, int, str]] = []

            def walk(node: ast.AST, prefix: str) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        q = f"{prefix}.{child.name}" if prefix else child.name
                        syms.append((child.lineno, child.end_lineno or child.lineno, q))
                        walk(child, q)
                    else:
                        walk(child, prefix)

            walk(self.tree, "")
            syms.sort()
            self._symbols = syms
        best = None
        for start, end, q in self._symbols:
            if start > lineno:
                break
            if start <= lineno <= end:
                if best is None or start >= best[0]:
                    best = (start, end, q)
        return best[2] if best else None

    def offset_to_line(self, base_line: int, text: str, pos: int) -> int:
        return base_line + text.count("\n", 0, pos)


def _collect_imports(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


class Project:
    """A source tree to analyse."""

    def __init__(
        self,
        root: str | os.PathLike,
        exclude_dirs: Iterable[str] = (),
        include_migrations: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        self.exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | set(exclude_dirs)
        self.include_migrations = include_migrations
        self._files: list[Path] | None = None
        self._parsed: dict[Path, ParsedModule | None] = {}
        self.errors: list[str] = []

    # ------------------------------------------------------------------ files
    def all_files(self) -> list[Path]:
        if self._files is None:
            out: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in self.exclude_dirs and not d.endswith(".egg-info")
                )
                for fn in sorted(filenames):
                    out.append(Path(dirpath) / fn)
            self._files = out
        return self._files

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def files(self, suffixes: Iterable[str], skip_migrations: bool | None = None) -> Iterator[Path]:
        suffixes = set(suffixes)
        skip = (not self.include_migrations) if skip_migrations is None else skip_migrations
        for p in self.all_files():
            if p.suffix.lower() in suffixes:
                if skip and is_migration(self.rel(p)):
                    continue
                yield p

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            try:
                return path.read_text(encoding="latin-1")
            except OSError:
                return None

    # ---------------------------------------------------------------- parsing
    def parse(self, path: Path) -> ParsedModule | None:
        path = path.resolve()
        if path in self._parsed:
            return self._parsed[path]
        src = self.read_text(path)
        mod = None
        if src is not None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    tree = ast.parse(src, filename=str(path))
                mod = ParsedModule(path, self.rel(path), src, tree, src.splitlines())
                mod.imports = _collect_imports(tree)
            except (SyntaxError, ValueError) as e:
                self.errors.append(f"{self.rel(path)}: cannot parse ({e.__class__.__name__})")
        self._parsed[path] = mod
        return mod

    def modules(self, skip_migrations: bool | None = None) -> Iterator[ParsedModule]:
        for p in self.files({".py"}, skip_migrations=skip_migrations):
            m = self.parse(p)
            if m is not None:
                yield m

    def module(self, rel: str) -> ParsedModule | None:
        return self.parse(self.root / rel)
