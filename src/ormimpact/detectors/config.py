"""Class-based configuration that names model fields in strings (layer: config).

Covers DRF serializers and viewsets, Django forms, admin, generic views,
django-filter, factory_boy factories, marshmallow(-sqlalchemy) schemas and the
options of the models themselves (Meta.ordering, constraints, indexes, ...).
"""

from __future__ import annotations

import ast

from ..astutil import (
    call_name, chain_root, class_assigns, const_str, dict_items, dotted, inner_class, iter_strings,
    kwarg, last_name,
)
from ..core import LAYER_CONFIG, Change, ModelInfo
from .base import Context, model_ref, strip_prefix

LIST_ATTRS = {
    "fields", "exclude", "read_only_fields", "write_only_fields", "list_display", "list_display_links",
    "list_filter", "list_editable", "search_fields", "ordering", "readonly_fields", "raw_id_fields",
    "autocomplete_fields", "list_select_related", "filter_horizontal", "filter_vertical", "sortable_by",
    "filterset_fields", "ordering_fields", "search_fields", "load_only", "dump_only", "unique_together",
    "index_together", "REQUIRED_FIELDS", "select_related_fields", "prefetch_related_fields",
    "required_fields", "only_fields", "exclude_fields",
}
DICT_KEY_ATTRS = {
    "extra_kwargs", "prepopulated_fields", "radio_fields", "labels", "help_texts", "widgets",
    "error_messages", "field_classes", "filterset_fields", "localized_fields",
}
DICT_VALUE_ATTRS = {"prepopulated_fields"}
STR_ATTRS = {
    "date_hierarchy", "lookup_field", "fk_name", "ordering", "get_latest_by", "order_with_respect_to",
    "slug_field", "USERNAME_FIELD", "EMAIL_FIELD", "sqla_session_field",
}
EXPLICIT_LIST_FOR_ADD = {"fields"}
SOURCE_KWARGS = ("source", "attribute")


def run(ctx: Context) -> None:
    if not len(ctx.registry):
        return
    registrations = _admin_registrations(ctx)
    for mod in ctx.project.modules():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.ClassDef) and node.name != "Meta":
                _Class(ctx, mod, node, registrations).run()


def _admin_registrations(ctx: Context) -> dict[str, list[ModelInfo]]:
    out: dict[str, list[ModelInfo]] = {}
    for mod in ctx.project.modules():
        if "register" not in mod.source:
            continue
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and last_name(node.func) == "register" and len(node.args) >= 2:
                cls = last_name(node.args[1])
                models = _models_in(ctx, node.args[0])
                if cls and models:
                    out.setdefault(cls, []).extend(models)
    return out


def _models_in(ctx: Context, node: ast.AST) -> list[ModelInfo]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [m for e in node.elts if (m := model_ref(ctx, e)) is not None]
    m = model_ref(ctx, node)
    return [m] if m else []


class _Class:
    def __init__(self, ctx: Context, mod, cls: ast.ClassDef, registrations) -> None:
        self.ctx, self.mod, self.cls, self.registrations = ctx, mod, cls, registrations
        self.meta = inner_class(cls, "Meta")
        self.bases = [dotted(b) or "" for b in cls.bases]

    def run(self) -> None:
        own = self.own_model()
        if own is not None:
            self.model_options(own)
            return
        for root in self.roots():
            self.config(root)

    # ----------------------------------------------------------- roots
    def own_model(self) -> ModelInfo | None:
        for m in self.ctx.registry.by_name.get(self.cls.name, []):
            if m.path == self.mod.rel and m.lineno == self.cls.lineno:
                return m
        return None

    def roots(self) -> list[ModelInfo]:
        roots: list[ModelInfo] = []
        scopes = [self.cls] + ([self.meta] if self.meta is not None else [])
        for scope in scopes:
            for name, value, _ in class_assigns(scope):
                if name == "model":
                    m = model_ref(self.ctx, value)
                    if m is not None:
                        roots.append(m)
                elif name == "queryset" and scope is self.cls:
                    m = self.ctx.model(chain_root(value))
                    if m is not None:
                        roots.append(m)
                elif name == "sqla_model":
                    m = model_ref(self.ctx, value)
                    if m:
                        roots.append(m)
        for dec in self.cls.decorator_list:
            if isinstance(dec, ast.Call) and last_name(dec.func) == "register":
                for a in dec.args:
                    roots.extend(_models_in(self.ctx, a))
        roots.extend(self.registrations.get(self.cls.name, []))
        seen, out = set(), []
        for r in roots:
            if id(r) not in seen:
                seen.add(id(r))
                out.append(r)
        return out

    @property
    def is_factory(self) -> bool:
        return any("Factory" in b for b in self.bases)

    @property
    def is_admin(self) -> bool:
        return any(b.split(".")[-1] in ("ModelAdmin", "TabularInline", "StackedInline", "GenericTabularInline",
                                         "GenericStackedInline") or "Admin" in b for b in self.bases)

    # --------------------------------------------------- config classes
    def config(self, root: ModelInfo) -> None:
        scopes = [self.cls] + ([self.meta] if self.meta is not None else [])
        declared: set[str] = set()
        explicit_fields: list[str] | None = None
        for scope in scopes:
            for name, value, stmt in class_assigns(scope):
                if name in LIST_ATTRS or name in STR_ATTRS:
                    if const_str(value) == "__all__":
                        self.all_fields(root, stmt)
                        continue
                    if name == "fields" and scope is self.meta or (name == "fields" and isinstance(value, (ast.List, ast.Tuple))):
                        explicit_fields = [s for s, _ in iter_strings(value)]
                    for s, node in iter_strings(value):
                        self.path(root, strip_prefix(s), node, f"{name}")
                if name in DICT_KEY_ATTRS:
                    for k, v in dict_items(value):
                        for s, node in iter_strings(k):
                            self.path(root, strip_prefix(s), node, f"{name}-key")
                        if name in DICT_VALUE_ATTRS:
                            for s, node in iter_strings(v):
                                self.path(root, strip_prefix(s), node, f"{name}-value")
                if name == "fieldsets":
                    for sub in ast.walk(value):
                        if isinstance(sub, ast.Dict):
                            for k, v in dict_items(sub):
                                if const_str(k) == "fields":
                                    for s, node in iter_strings(v):
                                        self.path(root, strip_prefix(s), node, "fieldsets")
                if scope is self.cls and name not in ("model", "queryset", "Meta") and (
                        isinstance(value, ast.Call) or self.is_factory):
                    declared.add(name)
                    self.declared_field(root, name, value, stmt)
        if self.is_factory:
            self.factory_adds(root, declared)
        elif explicit_fields is not None and not self.is_admin:
            for c in self.ctx.adds_for(root):
                self.ctx.add(self.mod, self.cls.lineno, LAYER_CONFIG, "fields-list-add", "medium", c,
                             evidence=f"class {self.cls.name}: fields = {explicit_fields[:6]}",
                             detail=f"explicit field list for {root.name}; the new field is not exposed")

    def all_fields(self, root: ModelInfo, stmt: ast.stmt) -> None:
        for c in self.ctx.changes:
            if c.model == root.name or c.model in root.bases:
                self.ctx.add(self.mod, stmt.lineno, LAYER_CONFIG, "all-fields", "medium", c,
                             detail=f"'__all__' exposes every field of {root.name}; the external contract changes")

    def declared_field(self, root: ModelInfo, name: str, value: ast.expr, stmt: ast.stmt) -> None:
        if name in LIST_ATTRS or name in STR_ATTRS or name in DICT_KEY_ATTRS or name == "fieldsets":
            return
        if not isinstance(value, ast.Call):
            for c, conf in self.ctx.by_attr(root, name):
                self.ctx.add(self.mod, stmt.lineno, LAYER_CONFIG, "factory-attribute", conf, c)
            return
        src = None
        for k in SOURCE_KWARGS:
            s = const_str(kwarg(value, k))
            if s:
                src = (s, kwarg(value, k))
                break
        if src is None and call_name(value) == "auto_field":
            s = const_str(value.args[0]) if value.args else const_str(kwarg(value, "column_name"))
            if s:
                src = (s, value)
        if src is not None:
            text, node = src
            if text != "*":
                self.path(root, text.replace(".", "__"), node, "declared-source")
            return
        if (call_name(value) or "") in ("SerializerMethodField", "HiddenField"):
            return
        # implicit source: a declared serializer/form/schema/factory attribute named like the field
        kind = "factory-attribute" if self.is_factory else "declared-field"
        for c, conf in self.ctx.by_attr(root, name):
            self.ctx.add(self.mod, stmt.lineno, LAYER_CONFIG, kind, conf, c)

    def factory_adds(self, root: ModelInfo, declared: set[str]) -> None:
        for c in self.ctx.adds_for(root):
            if c.required is False or c.field in declared or f"{c.field}_id" in declared:
                continue
            self.ctx.add(self.mod, self.cls.lineno, LAYER_CONFIG, "factory-missing-new-field", "medium", c,
                         evidence=f"class {self.cls.name}",
                         detail=f"factory for {root.name} does not set the new field {c.field!r}")

    # --------------------------------------------------- model options
    def model_options(self, model: ModelInfo) -> None:
        if self.meta is not None:
            for name, value, stmt in class_assigns(self.meta):
                if name in ("ordering", "unique_together", "index_together", "get_latest_by", "order_with_respect_to"):
                    for s, node in iter_strings(value):
                        self.path(model, strip_prefix(s), node, f"Meta.{name}")
                elif name in ("indexes", "constraints"):
                    for sub in ast.walk(value):
                        if not isinstance(sub, ast.Call):
                            continue
                        for kw in sub.keywords:
                            if kw.arg in ("fields", "include", "opclasses_fields"):
                                for s, node in iter_strings(kw.value):
                                    self.path(model, strip_prefix(s), node, f"Meta.{name}")
                        if call_name(sub) == "Q":
                            for kw in sub.keywords:
                                if kw.arg:
                                    self.path(model, kw.arg, kw, f"Meta.{name}-Q")
                        if call_name(sub) == "F":
                            for a in sub.args:
                                s = const_str(a)
                                if s:
                                    self.path(model, s, a, f"Meta.{name}-F")
        for name, value, stmt in class_assigns(self.cls):
            if name in ("REQUIRED_FIELDS", "USERNAME_FIELD", "EMAIL_FIELD"):
                for s, node in iter_strings(value):
                    self.path(model, s, node, name)
            if isinstance(value, ast.Call):
                f = model.fields.get(name)
                target = self.ctx.model(f.target) if f is not None and f.target else None
                if target is None:
                    continue
                tf = kwarg(value, "to_field")
                if const_str(tf):
                    self.path(target, const_str(tf) or "", tf, "to_field")
                for k, _ in dict_items(kwarg(value, "limit_choices_to")):
                    s = const_str(k)
                    if s:
                        self.path(target, s, k, "limit_choices_to")

    # ----------------------------------------------------------- report
    def path(self, root: ModelInfo, path: str, node, kind: str) -> None:
        if not path or not path.replace("__", "").replace("_", "a").isalnum():
            return
        for c, conf in self.ctx.by_path(root, path):
            self.ctx.add(self.mod, getattr(node, "lineno", self.cls.lineno), LAYER_CONFIG, kind, conf, c,
                         col=getattr(node, "col_offset", 0))
