"""What each code version maps, and the database operations of a release."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field

from ..astutil import call_name, const_str, dict_items, iter_strings, kwarg, last_name
from ..core import FieldInfo, ModelInfo
from ..project import ParsedModule, Project
from ..registry import DJANGO_RELATIONS, Registry


# --------------------------------------------------------------- mapping
@dataclass
class MappedColumn:
    table: str
    column: str
    model: str
    field: str
    nullable: bool
    db_default: bool
    py_default: bool
    col_type: str
    unique: bool
    path: str
    line: int


@dataclass
class TableMap:
    table: str
    model: ModelInfo
    columns: dict[str, MappedColumn] = dc_field(default_factory=dict)
    join_of: tuple[str, str] | None = None   # (model, m2m field) for join tables


def mapping(reg: Registry) -> dict[str, TableMap]:
    """Tables and columns that a code version's models map (what its ORM queries touch)."""
    out: dict[str, TableMap] = {}
    for m in reg.models:
        if m.abstract or not m.table:
            continue
        t = m.table.lower()
        tm = out.setdefault(t, TableMap(t, m))
        has_pk = False
        for f in m.fields.values():
            if f.kind == "ManyToManyField" or (f.is_relation and f.column is None and m.orm == "django"):
                jt = (f.m2m_table or f"{m.table}_{f.name}").lower()
                out.setdefault(jt, TableMap(jt, m, join_of=(m.name, f.name)))
                continue
            if not f.column:
                continue
            has_pk = has_pk or f.primary_key
            tm.columns[f.column.lower()] = MappedColumn(
                t, f.column.lower(), m.name, f.name, f.db_nullable, f.db_default, f.has_default, f.col_type,
                f.unique, m.path, f.lineno)
        if m.orm == "django" and not has_pk and "id" not in tm.columns:
            tm.columns["id"] = MappedColumn(t, "id", m.name, "id", False, True, True, "AutoField", True, m.path, m.lineno)
    return out


# ------------------------------------------------------------ operations
@dataclass
class DbOp:
    kind: str                     # create_table drop_table rename_table add_column drop_column rename_column
                                  # alter_column add_unique raw_sql data_migration
    table: str
    column: str | None = None
    new_name: str | None = None   # rename_column / rename_table
    nullable: bool | None = None  # add_column / alter_column: the new nullability
    db_default: bool | None = None
    col_type: str | None = None
    unique: bool | None = None
    model: str | None = None      # model name (best effort)
    field: str | None = None      # field name (best effort)
    path: str = ""
    line: int = 0
    text: str = ""                # source of the operation
    changes: dict[str, tuple] = dc_field(default_factory=dict)   # alter_column: attr -> (old, new)

    def describe(self) -> str:
        if self.kind == "add_column":
            flags = []
            flags.append("NULL" if self.nullable else "NOT NULL")
            if self.db_default:
                flags.append("DB default")
            return f"add column {self.table}.{self.column} ({', '.join(flags)})"
        if self.kind in ("drop_column",):
            return f"drop column {self.table}.{self.column}"
        if self.kind == "rename_column":
            return f"rename column {self.table}.{self.column} -> {self.new_name}"
        if self.kind == "alter_column":
            ch = ", ".join(f"{k}: {a} -> {b}" for k, (a, b) in self.changes.items())
            return f"alter column {self.table}.{self.column} ({ch})"
        if self.kind == "rename_table":
            return f"rename table {self.table} -> {self.new_name}"
        if self.kind == "add_unique":
            return f"add unique {self.table}({self.column})"
        if self.kind in ("raw_sql", "data_migration"):
            return f"{self.kind.replace('_', ' ')} in {self.path}"
        return f"{self.kind.replace('_', ' ')} {self.table}"


def is_migration_file(rel: str, text: str) -> str | None:
    """'django' | 'alembic' | None."""
    parts = rel.split("/")
    if not rel.endswith(".py") or parts[-1] == "__init__.py":
        return None
    if "migrations" in parts and "class Migration" in text:
        return "django"
    if ("versions" in parts or "alembic" in parts) and "def upgrade" in text and ("revision" in text):
        return "alembic"
    return None


class _Resolver:
    """Find model tables and fields for a migration across both versions."""

    def __init__(self, before: Registry, after: Registry) -> None:
        self.before, self.after = before, after

    def model(self, app: str | None, name: str, prefer_after: bool) -> ModelInfo | None:
        regs = (self.after, self.before) if prefer_after else (self.before, self.after)
        for reg in regs:
            if app:
                m = reg.by_label.get(f"{app}.{name}".lower())
                if m is not None:
                    return m
            for m in reg.models:
                if m.name.lower() == name.lower() and (not app or m.app_label == app):
                    return m
        for reg in regs:
            for m in reg.models:
                if m.name.lower() == name.lower():
                    return m
        return None

    def table(self, app: str | None, name: str, prefer_after: bool) -> str:
        m = self.model(app, name, prefer_after)
        if m is not None and m.table:
            return m.table.lower()
        return f"{app}_{name}".lower() if app else name.lower()

    def field(self, app, model_name, fname, after: bool) -> FieldInfo | None:
        reg = self.after if after else self.before
        for m in reg.models:
            if m.name.lower() == model_name.lower() and (not app or m.app_label == app or reg.by_label.get(f"{app}.{model_name}".lower()) is m):
                return m.fields.get(fname)
        return None


# -------------------------------------------------------------- Django
_POS = {
    "AddField": ("model_name", "name", "field"),
    "RemoveField": ("model_name", "name"),
    "RenameField": ("model_name", "old_name", "new_name"),
    "AlterField": ("model_name", "name", "field"),
    "CreateModel": ("name", "fields"),
    "DeleteModel": ("name",),
    "RenameModel": ("old_name", "new_name"),
    "AlterModelTable": ("name", "table"),
    "AddConstraint": ("model_name", "constraint"),
    "AlterUniqueTogether": ("name", "unique_together"),
    "RunSQL": ("sql", "reverse_sql"),
    "RunPython": ("code", "reverse_code"),
    "SeparateDatabaseAndState": ("database_operations", "state_operations"),
}


def _args(call: ast.Call, name: str) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for i, a in enumerate(call.args):
        names = _POS.get(name, ())
        if i < len(names):
            out[names[i]] = a
    for kw in call.keywords:
        if kw.arg:
            out[kw.arg] = kw.value
    return out


@dataclass
class _FieldProps:
    column: str | None
    nullable: bool
    db_default: bool
    col_type: str
    unique: bool
    m2m: bool
    m2m_table: str | None
    fk: bool


def _django_field_props(name: str, call: ast.AST | None) -> _FieldProps:
    if not isinstance(call, ast.Call):
        return _FieldProps(name, True, False, "", False, False, None, False)
    kind = last_name(call.func) or ""
    null = kwarg(call, "null")
    nullable = isinstance(null, ast.Constant) and null.value is True
    db_default = kwarg(call, "db_default") is not None or kind in ("AutoField", "BigAutoField", "SmallAutoField")
    unique = (isinstance(kwarg(call, "unique"), ast.Constant) and kwarg(call, "unique").value is True) or kind == "OneToOneField"
    rel = DJANGO_RELATIONS.get(kind)
    parts = []
    for k in ("max_length", "max_digits", "decimal_places"):
        v = kwarg(call, k)
        if isinstance(v, ast.Constant):
            parts.append(f"{k}={v.value}")
    col_type = f"{kind}({', '.join(parts)})" if parts else kind
    dbc = const_str(kwarg(call, "db_column"))
    if rel == "m2m":
        return _FieldProps(None, True, False, col_type, False, True, const_str(kwarg(call, "db_table")), False)
    column = dbc or (f"{name}_id" if rel else name)
    return _FieldProps(column, nullable, db_default, col_type, unique, False, None, bool(rel))


def _app_of(rel: str) -> str | None:
    parts = rel.split("/")
    if "migrations" in parts:
        i = parts.index("migrations")
        return parts[i - 1] if i > 0 else None
    return None


def parse_django(mod: ParsedModule, res: _Resolver) -> list[DbOp]:
    app = _app_of(mod.rel)
    ops_node = None
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "operations" for t in stmt.targets):
                    ops_node = stmt.value
    out: list[DbOp] = []
    if not isinstance(ops_node, (ast.List, ast.Tuple)):
        return out
    for op in ops_node.elts:
        out.extend(_django_op(op, app, mod, res))
    return out


def _django_op(op: ast.AST, app: str | None, mod: ParsedModule, res: _Resolver) -> list[DbOp]:
    if not isinstance(op, ast.Call):
        return []
    name = call_name(op) or ""
    a = _args(op, name)
    text = mod.snippet(op.lineno)
    base = dict(path=mod.rel, line=op.lineno, text=text)
    s = lambda k: const_str(a.get(k))  # noqa: E731
    if name == "SeparateDatabaseAndState":
        out = []
        dbops = a.get("database_operations")
        if isinstance(dbops, (ast.List, ast.Tuple)):
            for sub in dbops.elts:
                out.extend(_django_op(sub, app, mod, res))
        return out
    if name == "AddField":
        mn, fn = s("model_name"), s("name")
        if not mn or not fn:
            return []
        p = _django_field_props(fn, a.get("field"))
        model = res.model(app, mn, True)
        table = res.table(app, mn, True)
        if p.m2m:
            jt = (p.m2m_table or f"{table}_{fn}").lower()
            return [DbOp("create_table", jt, model=model.name if model else mn, field=fn, **base)]
        return [DbOp("add_column", table, p.column.lower() if p.column else fn, nullable=p.nullable,
                     db_default=p.db_default, col_type=p.col_type, unique=p.unique,
                     model=model.name if model else mn, field=fn, **base)]
    if name == "RemoveField":
        mn, fn = s("model_name"), s("name")
        if not mn or not fn:
            return []
        model = res.model(app, mn, False)
        table = res.table(app, mn, False)
        f = res.field(app, mn, fn, after=False)
        if f is not None and (f.kind == "ManyToManyField" or (f.is_relation and f.column is None)):
            jt = (f.m2m_table or f"{table}_{fn}").lower()
            return [DbOp("drop_table", jt, model=model.name if model else mn, field=fn, **base)]
        col = (f.column if f is not None and f.column else fn).lower()
        return [DbOp("drop_column", table, col, model=model.name if model else mn, field=fn, **base)]
    if name == "RenameField":
        mn, old, new = s("model_name"), s("old_name"), s("new_name")
        if not mn or not old or not new:
            return []
        table = res.table(app, mn, True)
        fb = res.field(app, mn, old, after=False)
        fa = res.field(app, mn, new, after=True)
        oc = (fb.column if fb is not None and fb.column else old).lower()
        nc = (fa.column if fa is not None and fa.column else new).lower()
        model = res.model(app, mn, True)
        if oc == nc:
            return []  # attribute renamed, column unchanged (db_column)
        return [DbOp("rename_column", table, oc, new_name=nc, model=model.name if model else mn, field=old, **base)]
    if name == "AlterField":
        mn, fn = s("model_name"), s("name")
        if not mn or not fn:
            return []
        new = _django_field_props(fn, a.get("field"))
        fb = res.field(app, mn, fn, after=False)
        table = res.table(app, mn, True)
        model = res.model(app, mn, True)
        changes: dict[str, tuple] = {}
        if fb is not None:
            if fb.db_nullable != new.nullable:
                changes["nullable"] = (fb.db_nullable, new.nullable)
            if fb.col_type and new.col_type and fb.col_type != new.col_type:
                changes["type"] = (fb.col_type, new.col_type)
            if fb.db_default != new.db_default:
                changes["db_default"] = (fb.db_default, new.db_default)
            if fb.unique != new.unique and new.unique:
                changes["unique"] = (fb.unique, new.unique)
            if fb.column and new.column and fb.column.lower() != new.column.lower():
                return [DbOp("rename_column", table, fb.column.lower(), new_name=new.column.lower(),
                             model=model.name if model else mn, field=fn, **base)]
        else:
            changes["unknown"] = ("?", new.col_type)
        if not changes:
            return []
        return [DbOp("alter_column", table, (new.column or fn).lower(), nullable=new.nullable,
                     db_default=new.db_default, col_type=new.col_type, model=model.name if model else mn,
                     field=fn, changes=changes, **base)]
    if name == "CreateModel":
        mn = s("name")
        if not mn:
            return []
        opts = a.get("options")
        table = None
        for k, v in dict_items(opts):
            if const_str(k) == "db_table":
                table = const_str(v)
        return [DbOp("create_table", (table or res.table(app, mn, True)).lower(), model=mn, **base)]
    if name == "DeleteModel":
        mn = s("name")
        return [DbOp("drop_table", res.table(app, mn, False), model=mn, **base)] if mn else []
    if name == "RenameModel":
        old, new = s("old_name"), s("new_name")
        if not old or not new:
            return []
        ot, nt = res.table(app, old, False), res.table(app, new, True)
        return [DbOp("rename_table", ot, new_name=nt, model=old, **base)] if ot != nt else []
    if name == "AlterModelTable":
        mn, t = s("name"), s("table")
        if not mn:
            return []
        ot = res.table(app, mn, False)
        nt = (t or f"{app}_{mn}").lower()
        return [DbOp("rename_table", ot, new_name=nt, model=mn, **base)] if ot != nt else []
    if name == "AddConstraint":
        mn = s("model_name")
        c = a.get("constraint")
        if mn and isinstance(c, ast.Call) and call_name(c) == "UniqueConstraint":
            cols = [x for x, _ in iter_strings(kwarg(c, "fields"))]
            return [DbOp("add_unique", res.table(app, mn, True), ",".join(cols), model=mn, **base)]
        return []
    if name == "AlterUniqueTogether":
        mn = s("name")
        cols = sorted({x for x, _ in iter_strings(a.get("unique_together"))})
        return [DbOp("add_unique", res.table(app, mn, True), ",".join(cols), model=mn, **base)] if mn and cols else []
    if name == "RunSQL":
        sql = a.get("sql")
        txt = " ".join(x for x, _ in iter_strings(sql)) if sql is not None else ""
        return [DbOp("raw_sql", "", text=txt[:300] or text, path=mod.rel, line=op.lineno)]
    if name == "RunPython":
        return [DbOp("data_migration", "", path=mod.rel, line=op.lineno, text=text)]
    return []


# -------------------------------------------------------------- Alembic
def _sa_column_props(call: ast.Call) -> tuple[str | None, bool, bool, str, bool]:
    name = const_str(call.args[0]) if call.args else const_str(kwarg(call, "name"))
    col_type = ""
    for x in call.args[1:]:
        if not const_str(x) and call_name(x) != "ForeignKey":
            col_type = ast.unparse(x)
            break
    nb = kwarg(call, "nullable")
    pk = isinstance(kwarg(call, "primary_key"), ast.Constant) and kwarg(call, "primary_key").value is True
    nullable = nb.value if isinstance(nb, ast.Constant) and isinstance(nb.value, bool) else not pk
    db_default = kwarg(call, "server_default") is not None or pk
    unique = isinstance(kwarg(call, "unique"), ast.Constant) and kwarg(call, "unique").value is True
    return name, nullable, db_default, col_type, unique


def parse_alembic(mod: ParsedModule, res: _Resolver) -> list[DbOp]:
    up = next((n for n in mod.tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    if up is None:
        return []
    batch_tables: dict[str, str] = {}
    for node in ast.walk(up):
        if isinstance(node, ast.With):
            for item in node.items:
                c = item.context_expr
                if isinstance(c, ast.Call) and call_name(c) == "batch_alter_table" and isinstance(item.optional_vars, ast.Name):
                    t = const_str(c.args[0]) if c.args else const_str(kwarg(c, "table_name"))
                    if t:
                        batch_tables[item.optional_vars.id] = t.lower()
    out: list[DbOp] = []
    for node in ast.walk(up):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        recv = node.func.value
        meth = node.func.attr
        args = list(node.args)
        if isinstance(recv, ast.Name) and recv.id in batch_tables:
            table = batch_tables[recv.id]
        elif isinstance(recv, ast.Name) and recv.id == "op":
            table = None
        else:
            continue
        base = dict(path=mod.rel, line=node.lineno, text=mod.snippet(node.lineno))
        t0 = table or (const_str(args[0]) if args else None)
        rest = args if table else args[1:]
        if meth == "add_column" and t0:
            col = rest[0] if rest else None
            if isinstance(col, ast.Call):
                n, nullable, dbd, ty, uq = _sa_column_props(col)
                if n:
                    out.append(DbOp("add_column", t0.lower(), n.lower(), nullable=nullable, db_default=dbd,
                                    col_type=ty, unique=uq, **base))
        elif meth == "drop_column" and t0:
            c = const_str(rest[0]) if rest else const_str(kwarg(node, "column_name"))
            if c:
                out.append(DbOp("drop_column", t0.lower(), c.lower(), **base))
        elif meth == "alter_column" and t0:
            c = const_str(rest[0]) if rest else const_str(kwarg(node, "column_name"))
            if not c:
                continue
            nn = const_str(kwarg(node, "new_column_name"))
            if nn and nn.lower() != c.lower():
                out.append(DbOp("rename_column", t0.lower(), c.lower(), new_name=nn.lower(), **base))
            changes: dict[str, tuple] = {}
            nb = kwarg(node, "nullable")
            if isinstance(nb, ast.Constant) and isinstance(nb.value, bool):
                ex = kwarg(node, "existing_nullable")
                old = ex.value if isinstance(ex, ast.Constant) else None
                changes["nullable"] = (old, nb.value)
            if kwarg(node, "type_") is not None:
                ex = kwarg(node, "existing_type")
                changes["type"] = (ast.unparse(ex) if ex is not None else "?", ast.unparse(kwarg(node, "type_")))
            if kwarg(node, "server_default") is not None:
                v = kwarg(node, "server_default")
                changes["db_default"] = ("?", not (isinstance(v, ast.Constant) and v.value is None))
            if changes:
                out.append(DbOp("alter_column", t0.lower(), (nn or c).lower(),
                                nullable=changes.get("nullable", (None, None))[1], changes=changes, **base))
        elif meth == "create_table" and t0:
            out.append(DbOp("create_table", t0.lower(), **base))
        elif meth == "drop_table" and t0:
            out.append(DbOp("drop_table", t0.lower(), **base))
        elif meth == "rename_table" and len(args) >= 2:
            a0, a1 = const_str(args[0]), const_str(args[1])
            if a0 and a1:
                out.append(DbOp("rename_table", a0.lower(), new_name=a1.lower(), **base))
        elif meth == "create_unique_constraint":
            t = const_str(args[1]) if len(args) > 1 else table
            cols = [x for x, _ in iter_strings(args[2] if len(args) > 2 else (args[1] if table and len(args) > 1 else None))]
            if t:
                out.append(DbOp("add_unique", t.lower(), ",".join(cols), **base))
        elif meth == "execute":
            txt = " ".join(x for x, _ in iter_strings(args[0])) if args else ""
            out.append(DbOp("raw_sql", "", text=txt[:300] or base["text"], path=mod.rel, line=node.lineno))
        elif meth in ("bulk_insert",):
            out.append(DbOp("data_migration", "", path=mod.rel, line=node.lineno, text=base["text"]))
    # attach model/field names where the table and column are mapped
    for op in out:
        for reg in (res.after, res.before):
            for m in reg.models:
                if m.table and m.table.lower() == op.table:
                    op.model = op.model or m.name
                    if op.column:
                        f = m.by_column(op.column) or m.fields.get(op.column)
                        if f is not None:
                            op.field = op.field or f.name
    return out


def release_ops(after: Project, migration_files: list[str], before_reg: Registry, after_reg: Registry) -> tuple[list[DbOp], list[str]]:
    res = _Resolver(before_reg, after_reg)
    ops: list[DbOp] = []
    notes: list[str] = []
    for rel in sorted(migration_files):
        mod = after.module(rel)
        if mod is None:
            notes.append(f"{rel}: cannot parse")
            continue
        kind = is_migration_file(rel, mod.source)
        if kind == "django":
            ops.extend(parse_django(mod, res))
        elif kind == "alembic":
            ops.extend(parse_alembic(mod, res))
    return ops, notes
