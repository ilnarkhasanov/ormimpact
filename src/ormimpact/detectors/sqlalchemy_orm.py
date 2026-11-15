"""SQLAlchemy string references and untyped query APIs (layer: orm)."""

from __future__ import annotations

import ast
import re

from ..astutil import call_name, chain_calls, chain_root, const_str, dict_items, iter_strings, kwarg, last_name
from ..core import LAYER_ORM, ModelInfo
from ..registry import SA_RELATION_CALLS, _ann_target, affects
from .base import Context, model_ref, strip_prefix

ROOT_CALLS = {"query", "select", "insert", "update", "delete", "get", "aliased", "with_entities"}
LOADER_OPTIONS = {
    "joinedload", "selectinload", "subqueryload", "lazyload", "load_only", "defer", "undefer",
    "contains_eager", "raiseload", "noload", "immediateload", "undefer_group",
}
CONSTRAINTS = {"Index", "UniqueConstraint", "PrimaryKeyConstraint", "ForeignKeyConstraint"}
REL_STRING_KWARGS = {"order_by", "primaryjoin", "secondaryjoin", "foreign_keys", "remote_side"}
DOTTED_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")


def run(ctx: Context) -> None:
    if "sqlalchemy" not in ctx.registry.orms:
        return
    names = {m.name for m in ctx.registry.models if m.orm == "sqlalchemy"}
    for mod in ctx.project.modules():
        if not ({"sqlalchemy", "flask_sqlalchemy", "sqlmodel"} & mod.imports) and not any(n in mod.source for n in names):
            continue
        _Visitor(ctx, mod).run()


class _Visitor:
    def __init__(self, ctx: Context, mod) -> None:
        self.ctx, self.mod = ctx, mod

    def run(self) -> None:
        for node in ast.walk(self.mod.tree):
            if isinstance(node, ast.ClassDef):
                self.model_class(node)
            elif isinstance(node, ast.Call):
                self.call(node)
            elif isinstance(node, ast.Attribute):
                self.table_c(node)
            elif isinstance(node, ast.Subscript):
                self.table_c_subscript(node)

    # ----------------------------------------------------- model classes
    def model_class(self, cls: ast.ClassDef) -> None:
        model = self.ctx.model(cls.name)
        for stmt in cls.body:
            value, ann = None, None
            if isinstance(stmt, ast.AnnAssign):
                value, ann = stmt.value, stmt.annotation
                tname = stmt.target.id if isinstance(stmt.target, ast.Name) else None
            elif isinstance(stmt, ast.Assign):
                value = stmt.value
                tname = stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else None
            else:
                continue
            if tname == "__table_args__" and model is not None:
                self.table_args(value, model)
                continue
            if not isinstance(value, ast.Call) or call_name(value) not in SA_RELATION_CALLS:
                continue
            tgt_node = value.args[0] if value.args else None
            target = model_ref(self.ctx, tgt_node) if tgt_node is not None else self.ctx.model(_ann_target(ann))
            bp = kwarg(value, "back_populates")
            s = const_str(bp)
            if s and target is not None:
                for c, conf in self.ctx.by_attr(target, s):
                    self.ctx.add(self.mod, bp.lineno, LAYER_ORM, "relationship-back_populates", conf, c, col=bp.col_offset)
            for kw in value.keywords:
                if kw.arg in REL_STRING_KWARGS:
                    for text, node in iter_strings(kw.value):
                        self.dotted_refs(text, node, f"relationship-{kw.arg}")

    def table_args(self, value: ast.AST | None, model: ModelInfo) -> None:
        if value is None:
            return
        for sub in ast.walk(value):
            if isinstance(sub, ast.Call) and call_name(sub) in CONSTRAINTS:
                args = sub.args[1:] if call_name(sub) == "Index" else sub.args
                for a in args:
                    for s, node in iter_strings(a):
                        self.column_ref(model, s, node, f"{call_name(sub)}-column")

    # ------------------------------------------------------------- calls
    def call(self, call: ast.Call) -> None:
        name = call_name(call)
        if name == "ForeignKey" and call.args:
            s = const_str(call.args[0])
            if s and "." in s:
                table, _, col = s.rpartition(".")
                m = self.ctx.registry.by_table.get(table.split(".")[-1].lower())
                if m is not None:
                    self.column_ref(m, col, call.args[0], "foreign-key-target")
            return
        if name in LOADER_OPTIONS:
            for a in call.args:
                for s, node in iter_strings(a):
                    for part in s.split("."):
                        for c, conf in self.ctx.by_name(part, "medium"):
                            self.ctx.add(self.mod, node.lineno, LAYER_ORM, f"{name}-string", conf, c,
                                         col=node.col_offset,
                                         detail="loader option with a string attribute name")
            return
        if name in ("bulk_insert_mappings", "bulk_update_mappings") and len(call.args) >= 2:
            root = model_ref(self.ctx, call.args[0])
            self.mapping_keys(call.args[1], root, name)
            return
        if name == "execute" and call.args and isinstance(call.args[0], ast.Call):
            root = self.root_of(call.args[0])
            if root is not None and len(call.args) >= 2:
                self.mapping_keys(call.args[1], root, "execute-params")
        if not isinstance(call.func, ast.Attribute):
            return
        method = call.func.attr
        if method == "filter_by":
            root = self.root_of(call.func.value)
            for kw in call.keywords:
                if kw.arg:
                    self.attr_ref(root, kw.arg, kw.value, "filter_by-kwarg", line_node=kw)
        elif method in ("values", "ordered_values"):
            root = self.root_of(call.func.value)
            if root is None:
                return
            for kw in call.keywords:
                if kw.arg:
                    self.attr_or_column(root, kw.arg, kw.value, "values-kwarg", line_node=kw)
            for a in call.args:
                self.mapping_keys(a, root, "values-dict")
        elif method in ("order_by", "group_by", "distinct"):
            root = self.root_of(call.func.value)
            for a in call.args:
                s = const_str(a)
                if s and re.fullmatch(r"-?[A-Za-z_][\w.]*", s):
                    if "." in s:
                        self.dotted_refs(s, a, f"{method}-string")
                    else:
                        self.attr_or_column(root, strip_prefix(s), a, f"{method}-string")

    def root_of(self, expr: ast.AST) -> ModelInfo | None:
        for c in chain_calls(expr):
            if call_name(c) in ROOT_CALLS and c.args:
                m = self.ctx.model(chain_root(c.args[0]))
                if m is not None and m.orm == "sqlalchemy":
                    return m
        m = self.ctx.model(chain_root(expr))
        return m if m is not None and m.orm == "sqlalchemy" else None

    # ------------------------------------------------- Model.__table__.c.x
    def table_c(self, node: ast.Attribute) -> None:
        inner = node.value
        if isinstance(inner, ast.Attribute) and inner.attr in ("c", "columns"):
            tab = inner.value
            if isinstance(tab, ast.Attribute) and tab.attr == "__table__":
                m = model_ref(self.ctx, tab.value)
                if m is not None:
                    self.column_ref(m, node.attr, node, "__table__.c")

    def table_c_subscript(self, node: ast.Subscript) -> None:
        inner = node.value
        s = const_str(node.slice)
        if s and isinstance(inner, ast.Attribute) and inner.attr in ("c", "columns"):
            tab = inner.value
            if isinstance(tab, ast.Attribute) and tab.attr == "__table__":
                m = model_ref(self.ctx, tab.value)
                if m is not None:
                    self.column_ref(m, s, node, "__table__.c[...]")

    # ----------------------------------------------------------- helpers
    def mapping_keys(self, node: ast.AST, root: ModelInfo | None, kind: str) -> None:
        dicts = [node] if isinstance(node, ast.Dict) else (
            [e for e in node.elts if isinstance(e, ast.Dict)] if isinstance(node, (ast.List, ast.Tuple)) else [])
        for d in dicts:
            for k, _ in dict_items(d):
                s = const_str(k)
                if s:
                    self.attr_or_column(root, s, k, kind)

    def attr_ref(self, root, name, node, kind, line_node=None) -> None:
        ln = getattr(line_node or node, "lineno", 0)
        for c, conf in self.ctx.by_attr(root, name, unknown_base="medium"):
            self.ctx.add(self.mod, ln, LAYER_ORM, kind, conf, c, col=getattr(node, "col_offset", 0),
                         detail=None if root else "query model not resolved statically; matched by name")

    def attr_or_column(self, root, name, node, kind, line_node=None) -> None:
        if root is not None and root.lookup(name) is None:
            f = root.by_column(name)
            if f is not None:
                self.column_ref(root, name, line_node or node, kind)
                return
        self.attr_ref(root, name, node, kind, line_node)

    def column_ref(self, model: ModelInfo, column: str, node, kind: str) -> None:
        f = model.by_column(column) or model.fields.get(column)
        if f is None:
            return
        for c in self.ctx.changes:
            if c.op != "add" and affects(c, model, f):
                conf = "medium" if c.op == "retype" else "high"
                self.ctx.add(self.mod, node.lineno, LAYER_ORM, kind, conf, c, col=node.col_offset)

    def dotted_refs(self, text: str, node, kind: str) -> None:
        for m in DOTTED_RE.finditer(text):
            model = self.ctx.model(m.group(1))
            if model is None:
                continue
            for c, conf in self.ctx.by_attr(model, m.group(2)):
                line = node.lineno + text.count("\n", 0, m.start())
                self.ctx.add(self.mod, line, LAYER_ORM, kind, conf, c, col=node.col_offset)
