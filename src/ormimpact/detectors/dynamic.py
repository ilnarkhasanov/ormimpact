"""Dynamic attribute access and untyped uses of model attributes (layer: dynamic).

* getattr / setattr / hasattr / delattr with a literal name
* model constructor keyword arguments (Order(total=...)), which SQLAlchemy's
  default ``__init__(**kwargs)`` hides from type checkers
* save(update_fields=[...]), refresh_from_db(fields=[...]), model_to_dict(...)
* attribute access in code a type checker skips: ``self.total`` inside the
  model's own methods and ``order.total`` on variables named after the model
  (mypy does not check the bodies of unannotated functions by default)
"""

from __future__ import annotations

import ast
import re

from ..astutil import call_name, const_str, iter_strings, kwarg, last_name
from ..core import LAYER_DYNAMIC, ModelInfo
from .base import Context, model_ref

ATTR_FUNCS = {"getattr", "setattr", "hasattr", "delattr"}
FIELD_LIST_KWARGS = {
    "save": ("update_fields",),
    "asave": ("update_fields",),
    "refresh_from_db": ("fields",),
    "arefresh_from_db": ("fields",),
    "model_to_dict": ("fields", "exclude"),
    "serialize": ("fields",),
    "full_clean": ("exclude",),
    "clean_fields": ("exclude",),
    "validate_unique": ("exclude",),
}


def _snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def var_model_map(ctx: Context) -> dict[str, ModelInfo]:
    """Variable names that conventionally hold a model instance: order, order_obj, new_order..."""
    out: dict[str, ModelInfo] = {}
    for m in ctx.registry.models:
        if m.abstract:
            continue
        sn = _snake(m.name)
        for v in (sn, m.name.lower(), f"{sn}_obj", f"{sn}_instance", f"new_{sn}", f"old_{sn}",
                  f"existing_{sn}", f"db_{sn}", f"{sn}_db", f"{sn}_model", f"the_{sn}"):
            out.setdefault(v, m)
    return out


def _guess(conf: str, guessed: bool) -> str:
    return "medium" if guessed and conf == "high" else conf


def run(ctx: Context) -> None:
    if not len(ctx.registry):
        return
    varmap = var_model_map(ctx)
    for mod in ctx.project.modules():
        _Visitor(ctx, mod, varmap).run()


class _Visitor:
    def __init__(self, ctx: Context, mod, varmap) -> None:
        self.ctx, self.mod, self.varmap = ctx, mod, varmap
        self.names = set()
        for c in ctx.changes:
            if c.op != "add":
                self.names |= ctx.change_names(c)

    def run(self) -> None:
        self.walk(self.mod.tree, None)

    def walk(self, node: ast.AST, cls_model: ModelInfo | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                m = None
                for cand in self.ctx.registry.by_name.get(child.name, []):
                    if cand.path == self.mod.rel and cand.lineno == child.lineno:
                        m = cand
                self.walk(child, m)
                continue
            if isinstance(child, ast.Call):
                self.call(child)
            elif isinstance(child, ast.Attribute):
                self.attribute(child, cls_model)
            self.walk(child, cls_model)

    # ------------------------------------------------------------ calls
    def call(self, call: ast.Call) -> None:
        name = call_name(call)
        if name in ATTR_FUNCS and isinstance(call.func, ast.Name) and len(call.args) >= 2:
            s = const_str(call.args[1])
            if s:
                root, guessed = self.root_for(call.args[0])
                for c, conf in self.ctx.by_attr(root, s, unknown_base="medium"):
                    conf = _guess(conf, guessed)
                    self.ctx.add(self.mod, call.lineno, LAYER_DYNAMIC, f"{name}-literal", conf, c,
                                 col=call.col_offset)
            return
        model = model_ref(self.ctx, call.func) if isinstance(call.func, (ast.Name, ast.Attribute)) else None
        if model is not None and (isinstance(call.func, ast.Name) or last_name(call.func) == model.name):
            for kw in call.keywords:
                if kw.arg:
                    for c, conf in self.ctx.by_attr(model, kw.arg):
                        self.ctx.add(self.mod, kw.value.lineno, LAYER_DYNAMIC, "constructor-kwarg", conf, c,
                                     col=kw.value.col_offset)
            for c in self.ctx.adds_for(model):
                if c.required is False:
                    continue
                given = {kw.arg for kw in call.keywords}
                if c.field not in given and f"{c.field}_id" not in given and not any(kw.arg is None for kw in call.keywords):
                    if call.keywords or call.args:
                        self.ctx.add(self.mod, call.lineno, LAYER_DYNAMIC, "constructor-missing-new-field",
                                     "medium", c, col=call.col_offset,
                                     detail=f"{model.name}(...) does not pass the new field {c.field!r}")
            return
        if name in FIELD_LIST_KWARGS:
            root, guessed = None, False
            if isinstance(call.func, ast.Attribute):
                root, guessed = self.root_for(call.func.value)
            elif name == "model_to_dict" and call.args:
                root, guessed = self.root_for(call.args[0])
            for k in FIELD_LIST_KWARGS[name]:
                for s, node in iter_strings(kwarg(call, k)):
                    for c, conf in self.ctx.by_attr(root, s, unknown_base="medium"):
                        conf = _guess(conf, guessed)
                        self.ctx.add(self.mod, node.lineno, LAYER_DYNAMIC, f"{name}-{k}", conf, c,
                                     col=node.col_offset)

    def root_for(self, node: ast.AST) -> tuple[ModelInfo | None, bool]:
        """(model, guessed): guessed is True when inferred from a variable name."""
        if isinstance(node, ast.Name) and node.id[:1].isupper():
            m = model_ref(self.ctx, node)
            if m is not None:
                return m, False
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name is None:
            return None, False
        m = self.varmap.get(name)
        return m, m is not None

    # -------------------------------------------------------- attributes
    def attribute(self, node: ast.Attribute, cls_model: ModelInfo | None) -> None:
        if node.attr not in self.names:
            return
        base = node.value
        if isinstance(base, ast.Name) and base.id in ("self", "cls") and cls_model is not None:
            for c, conf in self.ctx.by_attr(cls_model, node.attr):
                self.ctx.add(self.mod, node.lineno, LAYER_DYNAMIC, "self-attribute", conf, c,
                             col=node.col_offset, detail="attribute used inside the model class")
            return
        if isinstance(base, ast.Name) and base.id[:1].isupper():
            m = self.ctx.model(base.id)
            if m is not None:
                for c, conf in self.ctx.by_attr(m, node.attr):
                    self.ctx.add(self.mod, node.lineno, LAYER_DYNAMIC, "class-attribute", conf, c,
                                 col=node.col_offset)
            return
        root = None
        if isinstance(base, ast.Name):
            root = self.varmap.get(base.id)
        elif isinstance(base, ast.Attribute):
            root = self.varmap.get(base.attr)
        if root is None:
            return
        for c, conf in self.ctx.by_attr(root, node.attr):
            if conf == "high":
                conf = "medium"  # the variable name is a convention, not a type
            self.ctx.add(self.mod, node.lineno, LAYER_DYNAMIC, "attribute-by-variable-name", conf, c,
                         col=node.col_offset, detail=f"variable {ast.unparse(base)!r} assumed to hold {root.name}")
