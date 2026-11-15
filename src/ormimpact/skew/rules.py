"""Compatibility rules for the two version pairs of a deployment, and a linter baseline."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..core import (
    LAYER_CONFIG, LAYER_DATA, LAYER_DYNAMIC, LAYER_ORM, LAYER_SQL, LAYER_TEMPLATE, Change, FieldInfo, Finding,
)
from ..detectors.base import Context, Options
from ..detectors.sql import _Scanner, _Target
from ..engine import run_detectors
from ..project import Project
from ..registry import Registry
from .schema import DbOp, TableMap

PAIR_A = "old code on new schema"   # migrate first, rolling deploy, rollback
PAIR_B = "new code on old schema"   # deploy code first, migrate after

ORDERS = {
    "migrate, then deploy (rolling)": PAIR_A,
    "deploy, then migrate (post-deploy migration)": PAIR_B,
    "rollback of code after the migration": PAIR_A,
}

CREATION_KINDS = {
    "create-missing-new-field", "constructor-missing-new-field", "factory-missing-new-field",
    "fixture-missing-new-field", "fields-list-add", "all-fields", "sql-string", "sql-file",
}
TEST_PATH = re.compile(r"(^|/)(tests?|testing|tests?\.py|test_[^/]*\.py|[^/]*_tests?\.py|conftest\.py|factories[^/]*\.py)(/|$)")


@dataclass
class Evidence:
    path: str
    line: int
    text: str
    kind: str = ""


@dataclass
class Verdict:
    pair: str
    op: DbOp
    status: str                 # breaks | risky | safe
    confidence: str             # high | medium | low
    reason: str
    code_dependent: bool
    fix: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["op"] = {k: v for k, v in asdict(self.op).items() if v not in (None, "", {})}
        d["op"]["description"] = self.op.describe()
        return d


@dataclass
class Version:
    project: Project
    registry: Registry
    tables: dict[str, TableMap]


# ------------------------------------------------------------- linter
def linter_verdict(op: DbOp) -> tuple[bool, str]:
    """Operation-only rules in the style of django-migration-linter / strong_migrations."""
    if op.kind == "drop_column":
        return True, "DROP_COLUMN"
    if op.kind == "rename_column":
        return True, "RENAME_COLUMN"
    if op.kind == "drop_table":
        return True, "DROP_TABLE"
    if op.kind == "rename_table":
        return True, "RENAME_TABLE"
    if op.kind == "alter_column":
        return True, "ALTER_COLUMN"
    if op.kind == "add_column" and op.nullable is False and not op.db_default:
        return True, "NOT_NULL"
    if op.kind == "add_unique":
        return True, "ADD_UNIQUE"
    return False, ""


# ----------------------------------------------------------- evidence
def _findings(v: Version, changes: list[Change], layers: list[str]) -> list[Finding]:
    ctx = run_detectors(v.project, v.registry, changes, layers, Options(layers=layers))
    seen, out = set(), []
    for f in sorted(ctx.findings, key=lambda f: (-f.rank, f.path, f.line)):
        k = (f.path, f.line, f.kind)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def references(v: Version, model: str, fname: str) -> list[Finding]:
    """Uses of model.field in a code version (queries, config, dynamic access, SQL, templates, data)."""
    return _findings(v, [Change("remove", model, fname)],
                     [LAYER_ORM, LAYER_CONFIG, LAYER_DYNAMIC, LAYER_SQL, LAYER_TEMPLATE, LAYER_DATA])


def creation_sites(v: Version, model: str, column: str) -> list[Finding]:
    """Places where the code version creates rows of ``model`` without setting ``column``."""
    m = v.registry.get(model)
    if m is None:
        return []
    f = m.by_column(column)
    fname = f.name if f is not None else (column[:-3] if column.endswith("_id") and column[:-3] in m.fields else column)
    fs = _findings(v, [Change("add", model, fname, required=True)],
                   [LAYER_ORM, LAYER_CONFIG, LAYER_DYNAMIC, LAYER_DATA, LAYER_SQL])
    return [f for f in fs if f.kind in CREATION_KINDS or f.kind.endswith("missing-new-field")]


def raw_sql_refs(v: Version, table: str, column: str) -> list[Evidence]:
    """Raw SQL in a code version that names table.column (also when no model maps it)."""
    m = next((x for x in v.registry.models if x.table and x.table.lower() == table), None)
    if m is None:
        return []
    c = Change("remove", m.name, f"__col_{column}")
    ctx = Context(v.project, v.registry, [c], Options(layers=[LAYER_SQL]))
    scanner = _Scanner(ctx, [_Target(c, m, FieldInfo(c.field, "raw", column=column), column, table)],
                       set(v.registry.by_table))
    for mod in v.project.modules():
        scanner.module(mod)
    return [Evidence(f.path, f.line, f.evidence, f.kind) for f in ctx.findings]


def _ev(fs: list[Finding], limit: int = 8) -> list[Evidence]:
    return [Evidence(f.path, f.line, f.evidence, f"{f.layer}:{f.kind}") for f in fs[:limit]]


def _model_line(tm: TableMap | None, column: str | None) -> list[Evidence]:
    if tm is None:
        return []
    if column and column in tm.columns:
        mc = tm.columns[column]
        return [Evidence(mc.path, mc.line, f"{mc.model}.{mc.field} maps {mc.table}.{mc.column}", "model")]
    return [Evidence(tm.model.path, tm.model.lineno, f"{tm.model.name} maps table {tm.table}", "model")]


def _split_tests(fs: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    prod = [f for f in fs if not TEST_PATH.search(f.path)]
    test = [f for f in fs if TEST_PATH.search(f.path)]
    return prod, test


# ---------------------------------------------------------------- fixes
FIX = {
    "drop_column_A": "Split into two releases: first stop mapping the column (Django: SeparateDatabaseAndState "
                     "with state_operations=[RemoveField(...)] and no database operation; SQLAlchemy: remove the "
                     "attribute, keep the column), deploy; then drop the column in the next release.",
    "rename_column_A": "Expand/contract: add the new column, write both, backfill, switch reads, then drop the old "
                       "column; or keep the column name with db_column / Column('old_name').",
    "drop_table_A": "Remove the model from the code first (release N+1), drop the table in release N+2.",
    "add_not_null_A": "Add the column as nullable or with a database default (Django >= 5.0: db_default; "
                      "SQLAlchemy: server_default), backfill, and add NOT NULL in a later release.",
    "new_column_B": "Run the migration before the new code starts (migrate first); this change is not safe as a "
                    "post-deploy migration.",
    "drop_not_null_B": "Make the column nullable (or give it a database default) one release before the new code "
                       "stops writing it.",
    "rename_table_A": "Create the new table name as a view or keep db_table until the old version is gone.",
}


# --------------------------------------------------------------- rules
def evaluate(ops: list[DbOp], before: Version, after: Version) -> list[Verdict]:
    out: list[Verdict] = []
    for op in ops:
        out.append(_pair_a(op, before))
        out.append(_pair_b(op, before, after))
    return out


def _pair_a(op: DbOp, v: Version) -> Verdict:
    """Old code N runs on schema N+1."""
    tm = v.tables.get(op.table)
    if op.kind in ("drop_column", "rename_column"):
        col = op.column or ""
        fix = FIX["drop_column_A"] if op.kind == "drop_column" else FIX["rename_column_A"]
        if tm is not None and col in tm.columns:
            mc = tm.columns[col]
            refs = references(v, mc.model, mc.field)
            return Verdict(PAIR_A, op, "breaks", "high",
                           f"version N still maps {op.table}.{col} ({mc.model}.{mc.field}); the ORM selects every "
                           f"mapped column, so queries on {mc.model} fail once the column is gone"
                           + (f"; {len(refs)} more references to the field" if refs else ""),
                           True, fix, _model_line(tm, col) + _ev(refs))
        refs = raw_sql_refs(v, op.table, col)
        if refs:
            return Verdict(PAIR_A, op, "breaks", "medium",
                           f"no model of version N maps {op.table}.{col}, but its raw SQL names it", True, fix, refs)
        return Verdict(PAIR_A, op, "safe", "medium",
                       f"version N no longer maps {op.table}.{col} and no raw SQL names it", True)
    if op.kind in ("drop_table", "rename_table"):
        if tm is not None:
            if tm.join_of:
                refs = references(v, tm.join_of[0], tm.join_of[1])
                return Verdict(PAIR_A, op, "breaks", "high" if refs else "medium",
                               f"version N maps the many-to-many {tm.join_of[0]}.{tm.join_of[1]} through {op.table}",
                               True, FIX["drop_table_A"], _model_line(tm, None) + _ev(refs))
            return Verdict(PAIR_A, op, "breaks", "high",
                           f"version N still has the model {tm.model.name} on table {op.table}", True,
                           FIX["drop_table_A"] if op.kind == "drop_table" else FIX["rename_table_A"],
                           _model_line(tm, None))
        return Verdict(PAIR_A, op, "safe", "medium", f"version N does not map table {op.table}", True)
    if op.kind == "add_column":
        if op.nullable or op.db_default:
            return Verdict(PAIR_A, op, "safe", "high",
                           "nullable or database default: inserts by version N still succeed", False)
        if tm is None:
            return Verdict(PAIR_A, op, "safe", "medium", f"version N does not use table {op.table}", True)
        sites = creation_sites(v, tm.model.name, op.column or "")
        prod, test = _split_tests(sites)
        if prod:
            return Verdict(PAIR_A, op, "breaks", "high",
                           f"version N inserts {tm.model.name} rows without {op.column}, which becomes NOT NULL "
                           f"without a database default ({len(prod)} creation sites outside tests)",
                           True, FIX["add_not_null_A"], _ev(prod))
        if test:
            return Verdict(PAIR_A, op, "breaks", "medium",
                           f"only tests and fixtures of version N create {tm.model.name} rows; production code "
                           f"paths may still insert them (forms, admin, other services)", True,
                           FIX["add_not_null_A"], _ev(test))
        return Verdict(PAIR_A, op, "risky", "low",
                       f"NOT NULL without a database default on {op.table}, but no creation site was found in "
                       f"version N", True, FIX["add_not_null_A"])
    if op.kind == "alter_column":
        col = op.column or ""
        mc = tm.columns.get(col) if tm else None
        ch = op.changes
        if "nullable" in ch and ch["nullable"][1] is False and (mc is None or mc.nullable):
            if mc is not None and not mc.py_default and not op.db_default:
                # version N leaves the field NULL whenever it creates a row without setting it
                sites = creation_sites(v, mc.model, col)
                prod, test = _split_tests(sites)
                if prod or test:
                    return Verdict(PAIR_A, op, "breaks", "high" if prod else "medium",
                                   f"the column becomes NOT NULL, and version N creates {mc.model} rows without "
                                   f"setting {mc.field} (no default, so it writes NULL)", True,
                                   FIX["add_not_null_A"], _model_line(tm, col) + _ev(prod or test))
            return Verdict(PAIR_A, op, "risky", "medium",
                           "the column becomes NOT NULL while version N's model allows NULL", True,
                           FIX["add_not_null_A"], _model_line(tm, col))
        if "type" in ch:
            return Verdict(PAIR_A, op, "risky", "medium",
                           f"type changes {ch['type'][0]} -> {ch['type'][1]}; values written by version N may no "
                           f"longer fit", True, "", _model_line(tm, col))
        if "db_default" in ch and ch["db_default"][1] is False and mc is not None and not mc.py_default and not mc.nullable:
            return Verdict(PAIR_A, op, "risky", "low", "the database default is removed; version N may rely on it",
                           True)
        return Verdict(PAIR_A, op, "safe", "medium", "the change is compatible with version N's model", True)
    if op.kind == "add_unique":
        return Verdict(PAIR_A, op, "risky", "low", "version N may insert duplicates", False)
    if op.kind == "raw_sql":
        return Verdict(PAIR_A, op, "risky", "low", "raw SQL migration: review manually", False)
    if op.kind == "data_migration":
        return Verdict(PAIR_A, op, "safe", "low", "data migration: no schema change", False)
    return Verdict(PAIR_A, op, "safe", "high", "new objects are invisible to version N", False)


def _pair_b(op: DbOp, before: Version, after: Version) -> Verdict:
    """New code N+1 runs on schema N."""
    tm_a = after.tables.get(op.table)
    tm_b = before.tables.get(op.table)
    if op.kind == "add_column":
        if tm_a is not None and (op.column or "") in tm_a.columns:
            mc = tm_a.columns[op.column or ""]
            return Verdict(PAIR_B, op, "breaks", "high",
                           f"version N+1 maps {op.table}.{op.column} ({mc.model}.{mc.field}), which does not exist "
                           f"before the migration; every query on {mc.model} fails", True,
                           FIX["new_column_B"], _model_line(tm_a, op.column))
        return Verdict(PAIR_B, op, "safe", "medium", "version N+1 does not map the new column", True)
    if op.kind == "rename_column":
        if tm_a is not None and (op.new_name or "") in tm_a.columns:
            return Verdict(PAIR_B, op, "breaks", "high",
                           f"version N+1 maps {op.table}.{op.new_name}, which only exists after the migration", True,
                           FIX["new_column_B"], _model_line(tm_a, op.new_name))
        return Verdict(PAIR_B, op, "safe", "medium", "version N+1 does not map the renamed column", True)
    if op.kind in ("create_table", "rename_table"):
        target = op.new_name if op.kind == "rename_table" else op.table
        tm = after.tables.get(target or "")
        if tm is not None:
            who = f"{tm.join_of[0]}.{tm.join_of[1]}" if tm.join_of else tm.model.name
            return Verdict(PAIR_B, op, "breaks", "high" if op.kind == "rename_table" else "medium",
                           f"version N+1 uses {who} on table {target}, which does not exist yet", True,
                           FIX["new_column_B"], _model_line(tm, None))
        return Verdict(PAIR_B, op, "safe", "medium", "version N+1 does not map the table", True)
    if op.kind == "drop_column":
        mc = tm_b.columns.get(op.column or "") if tm_b else None
        if mc is not None and not mc.nullable and not mc.db_default and tm_a is not None:
            sites = creation_sites(after, tm_a.model.name, op.column or "")
            prod, test = _split_tests(sites)
            conf = "high" if prod else ("medium" if test else "low")
            status = "breaks" if (prod or test) else "risky"
            return Verdict(PAIR_B, op, status, conf,
                           f"{op.table}.{op.column} is NOT NULL without a database default until the migration runs, "
                           f"and version N+1 no longer sets it when inserting {tm_a.model.name} rows", True,
                           FIX["drop_not_null_B"], _model_line(tm_b, op.column) + _ev(prod or test))
        return Verdict(PAIR_B, op, "safe", "medium", "the dropped column is nullable or has a database default",
                       True)
    if op.kind == "alter_column":
        ch = op.changes
        mc_a = tm_a.columns.get(op.column or "") if tm_a else None
        if "nullable" in ch and ch["nullable"][1] is True and mc_a is not None and mc_a.nullable:
            return Verdict(PAIR_B, op, "risky", "medium",
                           "version N+1 may write NULL into a column that is NOT NULL until the migration runs", True,
                           FIX["new_column_B"], _model_line(tm_a, op.column))
        if "type" in ch:
            return Verdict(PAIR_B, op, "risky", "low",
                           f"version N+1 expects type {ch['type'][1]} before the migration has changed it", True)
        return Verdict(PAIR_B, op, "safe", "medium", "compatible with the old column", True)
    if op.kind == "drop_table":
        return Verdict(PAIR_B, op, "safe", "medium", "version N+1 no longer uses the table", False)
    if op.kind == "raw_sql":
        return Verdict(PAIR_B, op, "risky", "low", "raw SQL migration: review manually", False)
    return Verdict(PAIR_B, op, "safe", "medium", "no effect on version N+1", False)


def summarize(verdicts: list[Verdict]) -> dict:
    def worst(pair: str) -> str:
        st = [v.status for v in verdicts if v.pair == pair]
        return "breaks" if "breaks" in st else "risky" if "risky" in st else "safe"

    a, b = worst(PAIR_A), worst(PAIR_B)
    orders = {o: (a if p == PAIR_A else b) for o, p in ORDERS.items()}
    if a == "safe":
        rec = "migrate first, then deploy the code"
    elif b == "safe":
        rec = "deploy the code first, then run the migration (post-deploy migration)"
    else:
        rec = "no safe order: split the change into several releases (see the fixes)"
    return {"pair_a": a, "pair_b": b, "orders": orders, "recommendation": rec}
