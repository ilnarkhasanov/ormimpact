"""Django ORM query APIs: lookups in kwargs and strings (layer: orm)."""

from __future__ import annotations

import ast

from ..astutil import chain_attrs, chain_calls, chain_root, const_str, dict_items, iter_strings, last_name
from ..core import LAYER_ORM, ModelInfo
from .base import Context, model_ref, strip_prefix

KWARG_METHODS = {
    "filter", "exclude", "get", "get_or_create", "update_or_create", "create", "update",
    "aggregate", "annotate", "alias", "aget", "acreate", "aget_or_create", "aupdate_or_create",
    "aupdate", "in_bulk", "afilter",
}
STRING_METHODS = {
    "order_by", "values", "values_list", "only", "defer", "select_related", "prefetch_related",
    "distinct", "latest", "earliest", "alatest", "aearliest", "dates", "datetimes", "in_bulk",
    "bulk_update", "abulk_update", "select_for_update",
}
STRONG_METHODS = {
    "filter", "exclude", "get_or_create", "update_or_create", "order_by", "values", "values_list",
    "only", "defer", "select_related", "prefetch_related", "annotate", "aggregate", "alias",
}
QS_CHAIN_ATTRS = {
    "objects", "_default_manager", "_base_manager", "all", "none", "using", "filter", "exclude",
    "order_by", "select_related", "prefetch_related", "annotate", "distinct", "only", "defer",
    "values", "values_list", "select_for_update", "all_objects",
}
# expressions whose string arguments are lookup paths
EXPR_STRING_FUNCS = {
    "F", "OuterRef", "Count", "Sum", "Avg", "Max", "Min", "StdDev", "Variance", "ArrayAgg",
    "StringAgg", "JSONBAgg", "BoolAnd", "BoolOr", "Coalesce", "Concat", "Lower", "Upper",
    "Length", "Trim", "Cast", "Greatest", "Least", "Trunc", "TruncDate", "TruncDay", "TruncMonth",
    "TruncYear", "TruncWeek", "TruncHour", "Extract", "ExtractYear", "ExtractMonth", "ExtractDay",
    "Prefetch", "FilteredRelation", "Collate", "NullIf", "Abs", "Round", "Left", "Right",
    "Substr", "Replace", "Reverse", "SearchVector", "Upper", "Exists",
}
EXPR_KWARG_FUNCS = {"Q"}
# the first positional argument is a *name* to define, not a lookup
NAME_FIRST_FUNCS = {"Extract"}
STRING_ARGS_SKIP = {"select_for_update": {"of"}}


def run(ctx: Context) -> None:
    if "django" not in ctx.registry.orms:
        return
    for mod in ctx.project.modules():
        if "django" not in mod.imports and not _mentions_models(ctx, mod):
            continue
        _Visitor(ctx, mod).run()


def _mentions_models(ctx: Context, mod) -> bool:
    src = mod.source
    return any(m.name in src for m in ctx.registry.models if m.orm == "django")


class _Visitor:
    def __init__(self, ctx: Context, mod) -> None:
        self.ctx = ctx
        self.mod = mod
        self.expr_root: dict[int, ModelInfo] = {}
        self.done: set[int] = set()

    def run(self) -> None:
        calls = [n for n in ast.walk(self.mod.tree) if isinstance(n, ast.Call)]
        # first pass: queryset calls with a known root give context to nested expressions
        for call in calls:
            root, _ = self.qs_root(call)
            if root is not None:
                for sub in ast.walk(call):
                    if isinstance(sub, ast.Call) and sub is not call:
                        self.expr_root.setdefault(id(sub), root)
        for call in calls:
            self.visit_call(call)

    # ------------------------------------------------------------- roots
    def qs_root(self, call: ast.Call) -> tuple[ModelInfo | None, str]:
        """(model, how) for a queryset method call, how in {'model','accessor','unknown',''}."""
        if not isinstance(call.func, ast.Attribute):
            return None, ""
        method = call.func.attr
        if method not in KWARG_METHODS and method not in STRING_METHODS:
            return None, ""
        receiver = call.func.value
        root = self.ctx.model(chain_root(receiver))
        if root is not None and root.orm == "django":
            return root, "model"
        attrs = chain_attrs(receiver)
        # related manager: customer.orders.filter(...)
        for a in attrs:
            if a in QS_CHAIN_ATTRS:
                continue
            tgt = self._accessor_target(a)
            if tgt is not None:
                return tgt, "accessor"
            break
        if method in STRONG_METHODS or any(a in QS_CHAIN_ATTRS for a in attrs):
            return None, "unknown"
        return None, ""

    def _accessor_target(self, name: str) -> ModelInfo | None:
        owners = self.ctx.registry.accessors.get(name, [])
        targets = set()
        for o in owners:
            f = o.lookup(name)
            if f is not None and f.target:
                targets.add(f.target)
        if len(targets) == 1:
            m = self.ctx.model(next(iter(targets)))
            if m is not None and m.orm == "django":
                return m
        return None

    # ------------------------------------------------------------ visits
    def visit_call(self, call: ast.Call) -> None:
        if id(call) in self.done:
            return
        name = last_name(call.func)
        root, how = self.qs_root(call)
        if how:
            self.queryset_call(call, root, how)
            return
        if name in EXPR_KWARG_FUNCS or name in EXPR_STRING_FUNCS:
            self.expression(call, name, self.expr_root.get(id(call)))
            return
        if name == "get_field" and isinstance(call.func, ast.Attribute):
            # Order._meta.get_field("total")
            meta = call.func.value
            if isinstance(meta, ast.Attribute) and meta.attr == "_meta":
                root = model_ref(self.ctx, meta.value)
                for s, node in iter_strings(call.args[0] if call.args else None):
                    self.report(node, root, s, "meta-get-field", unknown="low", sep="\0")

    def queryset_call(self, call: ast.Call, root: ModelInfo | None, how: str) -> None:
        method = call.func.attr  # type: ignore[union-attr]
        unknown = "medium" if how == "unknown" else "medium"
        base_kind = f"{method}"
        if method in KWARG_METHODS:
            for kw in call.keywords:
                if kw.arg is None:
                    for k, _v in dict_items(kw.value):  # filter(**{"total__gt": 1})
                        s = const_str(k)
                        if s:
                            self.report(k, root, s, f"{base_kind}-kwarg", unknown, how=how)
                    continue
                if kw.arg in ("defaults", "create_defaults"):
                    for k, _v in dict_items(kw.value):
                        s = const_str(k)
                        if s:
                            self.report(k, root, s, f"{base_kind}-defaults", unknown, how=how)
                    continue
                if method in ("annotate", "alias", "aggregate"):
                    continue  # kwarg names are new aliases; expressions are visited separately
                if method == "in_bulk" and kw.arg == "field_name":
                    s = const_str(kw.value)
                    if s:
                        self.report(kw.value, root, s, "in_bulk-field", unknown, how=how)
                    continue
                self.report(kw.value, root, kw.arg, f"{base_kind}-kwarg", unknown, how=how, line_node=kw)
            if method == "create" and root is not None:
                self.creation_site(call, root)
        if method in STRING_METHODS:
            args = call.args
            if method in ("bulk_update", "abulk_update"):
                args = call.args[1:2] + [kw.value for kw in call.keywords if kw.arg == "fields"]
            elif method == "in_bulk":
                args = []
            elif method == "values_list":
                args = list(call.args)
            for a in args:
                for s, node in iter_strings(a):
                    self.report(node, root, strip_prefix(s), f"{base_kind}-string", unknown, how=how)
        # nested Q/F/... with this root
        for a in list(call.args) + [kw.value for kw in call.keywords]:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Call) and last_name(sub.func) in EXPR_KWARG_FUNCS | EXPR_STRING_FUNCS:
                    self.expression(sub, last_name(sub.func) or "", root)
                    self.done.add(id(sub))

    def expression(self, call: ast.Call, name: str, root: ModelInfo | None) -> None:
        if id(call) in self.done:
            return
        self.done.add(id(call))
        if name in EXPR_KWARG_FUNCS:
            for kw in call.keywords:
                if kw.arg and kw.arg not in ("_connector", "_negated"):
                    self.report(kw.value, root, kw.arg, f"{name}-kwarg", "low", line_node=kw)
            return
        args = call.args[1:] if name in NAME_FIRST_FUNCS else call.args
        for a in args:
            s = const_str(a)
            if s is not None:
                self.report(a, root, strip_prefix(s), f"{name}-string", "low")
        if name == "Prefetch" or name == "FilteredRelation":
            for kw in call.keywords:
                if kw.arg in ("lookup", "relation_name"):
                    s = const_str(kw.value)
                    if s:
                        self.report(kw.value, root, s, f"{name}-string", "low")

    def creation_site(self, call: ast.Call, root: ModelInfo) -> None:
        for c in self.ctx.adds_for(root):
            if c.required is False:
                continue
            given = {kw.arg for kw in call.keywords if kw.arg}
            if c.field not in given and f"{c.field}_id" not in given and not any(kw.arg is None for kw in call.keywords):
                self.ctx.add(self.mod, call.lineno, LAYER_ORM, "create-missing-new-field",
                             "medium", c, col=call.col_offset,
                             detail=f"{root.name} is created without the new field {c.field!r}")

    # ------------------------------------------------------------ report
    def report(self, node, root, path: str, kind: str, unknown: str, how: str = "model",
               sep: str = "__", line_node=None) -> None:
        if not path or " " in path or "(" in path:
            return
        matches = self.ctx.by_path(root, path, sep=sep, unknown_base=unknown)
        if how == "accessor":
            matches = [(c, "medium" if conf == "high" else conf) for c, conf in matches]
        ln = getattr(line_node or node, "lineno", 0) or getattr(node, "lineno", 0)
        col = getattr(line_node or node, "col_offset", 0)
        for c, conf in matches:
            detail = None
            if root is None:
                detail = "model of the queryset not resolved statically; matched by name"
            elif how == "accessor":
                detail = f"queryset model inferred from related manager ({root.name})"
            self.ctx.add(self.mod, ln, LAYER_ORM, kind, conf, c, col=col, detail=detail)
