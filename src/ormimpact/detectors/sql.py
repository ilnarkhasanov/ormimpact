"""Raw SQL in Python strings and .sql files (layer: sql).

Uses sqlglot to resolve tables, aliases and columns when it is installed and
falls back to token matching otherwise.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from ..astutil import call_name
from ..core import LAYER_SQL, Change, FieldInfo, ModelInfo
from ..project import SQL_SUFFIXES
from .base import Context

SQL_FUNCS = {
    "text", "execute", "executemany", "raw", "RunSQL", "exec_driver_sql", "extra", "literal_column",
    "read_sql", "read_sql_query", "executescript", "mogrify", "copy_expert", "RawSQL", "fetch",
    "fetchrow", "fetchval", "execute_values",
}
SQL_RE = re.compile(
    r"\b(select\b[\s\S]{1,2000}?\bfrom\b|insert\s+into\b|update\s+[\w.\"`]+\s+set\b|delete\s+from\b|"
    r"alter\s+table\b|create\s+(?:unique\s+)?index\b)",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLACEHOLDER = "__ormimpact_param__"


@dataclass
class _Target:
    change: Change
    model: ModelInfo
    field: FieldInfo
    column: str
    table: str


def _targets(ctx: Context) -> list[_Target]:
    out = []
    for c in ctx.changes:
        m = ctx.registry.get(c.model)
        if m is None:
            continue
        if c.op == "add":
            if m.table:
                out.append(_Target(c, m, FieldInfo(c.field, "new", column=c.field), c.field.lower(), m.table.lower()))
            continue
        f = m.fields.get(c.field)
        if f is None or not f.column:
            continue
        tables = []
        if m.table and not m.abstract:
            tables.append(m.table)
        else:  # abstract base: every concrete subclass table
            for sub in ctx.registry.models:
                if not sub.abstract and sub.table and c.field in sub.fields and sub.fields[c.field].inherited_from == m.name:
                    tables.append(sub.table)
        for t in tables:
            out.append(_Target(c, m, f, f.column.lower(), t.lower()))
    return out


def run(ctx: Context) -> None:
    targets = _targets(ctx)
    if not targets:
        return
    known_tables = {t.lower() for t in ctx.registry.by_table}
    scanner = _Scanner(ctx, targets, known_tables)
    for mod in ctx.project.modules():
        scanner.module(mod)
    for path in ctx.project.files(SQL_SUFFIXES):
        text = ctx.project.read_text(path)
        if text:
            scanner.text(ctx.project.rel(path), text, 1, file_level=True)


class _Scanner:
    def __init__(self, ctx: Context, targets: list[_Target], known_tables: set[str]) -> None:
        self.ctx, self.targets, self.known_tables = ctx, targets, known_tables
        self.columns = {t.column for t in targets}
        try:
            import sqlglot  # noqa: F401

            self.sqlglot = ctx.options.sql_parser
        except ImportError:
            self.sqlglot = False

    # --------------------------------------------------------- python files
    def module(self, mod) -> None:
        low = mod.source.lower()
        if not any(c in low for c in self.columns):
            return
        sql_args: set[int] = set()
        docstrings: set[int] = set()
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and call_name(node) in SQL_FUNCS:
                for a in list(node.args) + [kw.value for kw in node.keywords]:
                    sql_args.add(id(a))
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        inside_joined: set[int] = set()
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.JoinedStr):
                for v in node.values:
                    inside_joined.add(id(v))
        for node in ast.walk(mod.tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside_joined:
                if id(node) in docstrings:
                    continue
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                parts = []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    else:
                        parts.append(PLACEHOLDER)
                text = "".join(parts)
            if not text or len(text) < 8:
                continue
            explicit = id(node) in sql_args
            if not explicit and not SQL_RE.search(text):
                continue
            self.text(mod, text, node.lineno, explicit=explicit)

    # ---------------------------------------------------------------- text
    def text(self, where, text: str, base_line: int, explicit: bool = False, file_level: bool = False) -> None:
        low = text.lower()
        if not any(re.search(rf"\b{re.escape(c)}\b", low) for c in self.columns) and not any(
            t.change.op == "add" for t in self.targets
        ):
            return
        offset = 0
        for stmt in (text.split(";") if file_level else [text]):
            if stmt.strip():
                self.statement(where, stmt, base_line + text.count("\n", 0, offset), explicit or file_level)
            offset += len(stmt) + 1

    def statement(self, where, sql: str, base_line: int, explicit: bool) -> None:
        parsed = self.parse(sql) if self.sqlglot else None
        tokens = {t.lower() for t in TOKEN_RE.findall(sql)}
        for t in self.targets:
            if t.change.op == "add":
                self.insert_without(where, sql, base_line, t, parsed)
                continue
            if t.column not in tokens:
                continue
            conf = None
            how = ""
            if parsed is not None:
                tables, aliases, cols = parsed
                for qual, name in cols:
                    if name != t.column:
                        continue
                    if qual:
                        resolved = aliases.get(qual, qual)
                        if resolved == t.table:
                            conf, how = "high", f"{qual}.{name} resolves to table {t.table}"
                        elif resolved in self.known_tables or resolved in tables:
                            continue
                        else:
                            conf, how = conf or "medium", f"column {name} with unresolved qualifier {qual}"
                    elif t.table in tables:
                        conf = conf or ("high" if len(tables) == 1 else "medium")
                        how = how or f"column {name} in a statement on {', '.join(sorted(tables))}"
                    elif not tables:
                        conf, how = conf or "low", how or f"column {name}, no table found"
                if conf is None and parsed[0] and t.table in parsed[0] and t.column in tokens:
                    conf, how = "medium", f"{t.column} appears in a statement on {t.table}"
            else:
                if t.table in tokens:
                    conf, how = "medium", f"column {t.column} and table {t.table} appear in the statement"
                elif explicit and len(t.column) >= 4:
                    conf, how = "low", f"column {t.column} appears in SQL; table not found"
            if conf is None:
                continue
            if t.change.op == "retype":
                conf = {"high": "medium", "medium": "low", "low": "low"}[conf]
            m = re.search(rf"\b{re.escape(t.column)}\b", sql, re.IGNORECASE)
            line = base_line + (sql.count("\n", 0, m.start()) if m else 0)
            self.add(where, line, t, conf, how, sql)

    def insert_without(self, where, sql, base_line, t: _Target, parsed) -> None:
        if t.change.required is False:
            return
        m = re.search(rf"insert\s+into\s+[\"`]?{re.escape(t.table)}[\"`]?\s*\(([^)]*)\)", sql, re.IGNORECASE)
        if not m:
            return
        cols = {c.strip().strip('"`').lower() for c in m.group(1).split(",")}
        if t.column in cols:
            return
        line = base_line + sql.count("\n", 0, m.start())
        self.add(where, line, t, "medium", f"INSERT into {t.table} lists columns without the new {t.column}", sql)

    def add(self, where, line, t: _Target, conf, how, sql) -> None:
        if isinstance(where, str):
            snippet = " ".join(sql.strip().split())[:160]
            self.ctx.add(where, line, LAYER_SQL, "sql-file", conf, t.change, evidence=snippet, detail=how)
        else:
            self.ctx.add(where, line, LAYER_SQL, "sql-string", conf, t.change, detail=how)

    # ------------------------------------------------------------- parsing
    def parse(self, sql: str):
        import sqlglot
        from sqlglot import exp

        import logging

        logging.getLogger("sqlglot").setLevel(logging.ERROR)
        clean = sql.replace(PLACEHOLDER, "1")
        clean = re.sub(r"%\(\w+\)s|%s|:\w+|\?|\$\d+", "1", clean)
        try:
            trees = sqlglot.parse(clean, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            return None
        tables: set[str] = set()
        aliases: dict[str, str] = {}
        cols: list[tuple[str, str]] = []
        any_tree = False
        for tree in trees:
            if tree is None:
                continue
            any_tree = True
            for tab in tree.find_all(exp.Table):
                name = (tab.name or "").lower()
                if not name:
                    continue
                tables.add(name)
                alias = (tab.alias or "").lower()
                if alias:
                    aliases[alias] = name
            for col in tree.find_all(exp.Column):
                cols.append(((col.table or "").lower(), (col.name or "").lower()))
            for schema in tree.find_all(exp.Schema):
                tname = schema.this.name.lower() if isinstance(schema.this, exp.Table) else ""
                for e in schema.expressions:
                    if isinstance(e, exp.Identifier):
                        cols.append((tname, e.name.lower()))
        if not any_tree:
            return None
        return tables, aliases, cols
