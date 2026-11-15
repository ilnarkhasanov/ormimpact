"""Discovery of ORM models (Django, SQLAlchemy incl. Flask-SQLAlchemy and SQLModel).

Models are found statically with ``ast``; the analysed project does not have
to be importable.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .astutil import call_name, class_assigns, const_str, dotted, inner_class, kwarg, last_name, span
from .core import Change, FieldInfo, ModelInfo
from .project import ParsedModule, Project

# ----------------------------------------------------------------- Django
DJANGO_RELATIONS = {
    "ForeignKey": "fk",
    "OneToOneField": "o2o",
    "ManyToManyField": "m2m",
    "ParentalKey": "fk",
    "ParentalManyToManyField": "m2m",
    "TreeForeignKey": "fk",
    "TreeOneToOneField": "o2o",
    "TreeManyToManyField": "m2m",
}
DJANGO_OTHER_FIELDS = {"GenericForeignKey", "GenericRelation"}
# fields whose implicit default is "" (empty_strings_allowed), so instances can be created without them
DJANGO_STRING_FIELDS = {
    "CharField", "TextField", "EmailField", "SlugField", "URLField", "FilePathField", "FileField",
    "ImageField", "GenericIPAddressField", "IPAddressField", "CICharField", "CITextField", "CIEmailField",
}
DJANGO_MODEL_BASES = {"models.Model", "Model", "django.db.models.Model", "db.models.Model"}
NOT_DJANGO_PREFIXES = ("serializers.", "forms.", "filters.", "fields.", "ma.", "factory.", "pydantic.")

# ------------------------------------------------------------- SQLAlchemy
SA_COLUMN_CALLS = {
    "Column", "mapped_column", "column_property", "deferred", "composite",
    "synonym", "query_expression", "association_proxy", "hybrid_property",
}
SA_RELATION_CALLS = {"relationship", "relation", "dynamic_loader", "Relationship"}
SA_MAPPED = {"Mapped", "WriteOnlyMapped", "DynamicMapped"}
SA_DECL_BASES = {"DeclarativeBase", "DeclarativeBaseNoMeta", "AsyncAttrs", "MappedAsDataclass"}
SA_IMPORTS = {"sqlalchemy", "flask_sqlalchemy", "sqlmodel"}


def _snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def _app_label(rel: str) -> str | None:
    parts = rel.split("/")
    if len(parts) >= 3 and parts[-2] == "models":
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return None


def _target_name(node: ast.AST | None, own: str) -> str | None:
    if node is None:
        return None
    s = const_str(node)
    if s is not None:
        if s == "self":
            return own
        return s.split(".")[-1]
    n = last_name(node)
    if n:
        return n
    if isinstance(node, ast.Lambda):
        return _target_name(node.body, own)
    return None


def _bool_kw(call: ast.Call, name: str) -> bool | None:
    v = kwarg(call, name)
    if isinstance(v, ast.Constant) and isinstance(v.value, bool):
        return v.value
    return None


@dataclass
class _RawClass:
    name: str
    node: ast.ClassDef
    module: ParsedModule
    bases: list[str]
    orm: str | None = None
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    table: str | None = None
    app_label: str | None = None
    abstract: bool = False
    is_decl_base: bool = False


class Registry:
    """All models of a project, with relations linked."""

    def __init__(self, models: list[ModelInfo]):
        self.models = models
        self.by_name: dict[str, list[ModelInfo]] = defaultdict(list)
        self.by_table: dict[str, ModelInfo] = {}
        self.by_label: dict[str, ModelInfo] = {}
        for m in models:
            self.by_name[m.name].append(m)
            if m.table and not m.abstract:
                self.by_table.setdefault(m.table.lower(), m)
            self.by_label.setdefault(m.label.lower(), m)
        self.field_owners: dict[str, list[ModelInfo]] = defaultdict(list)
        self.accessors: dict[str, list[ModelInfo]] = defaultdict(list)
        self._link()

    # ---------------------------------------------------------------- query
    def get(self, name: str | None) -> ModelInfo | None:
        if not name:
            return None
        ms = self.by_name.get(name)
        if not ms:
            return None
        concrete = [m for m in ms if not m.abstract]
        return (concrete or ms)[0]

    def model_files(self) -> set[str]:
        return {m.path for m in self.models}

    @property
    def orms(self) -> set[str]:
        return {m.orm for m in self.models}

    def __len__(self) -> int:
        return len(self.models)

    def _link(self) -> None:
        for m in self.models:
            for f in m.fields.values():
                self.field_owners[f.name].append(m)
                if f.is_relation:
                    self.accessors[f.name].append(m)
        for m in list(self.models):
            if m.abstract:
                continue
            for f in list(m.fields.values()):
                if not f.is_relation or not f.target:
                    continue
                tgt = self.get(f.target)
                if tgt is None:
                    continue
                names = []
                if f.reverse_name and f.reverse_name != "+" and not f.reverse_name.endswith("+"):
                    names.append(f.reverse_name.replace("%(class)s", m.name.lower()))
                if f.reverse_query_name and f.reverse_query_name not in names:
                    names.append(f.reverse_query_name.replace("%(class)s", m.name.lower()))
                for rn in names:
                    if rn in tgt.fields or rn in tgt.reverse:
                        continue
                    rev = FieldInfo(
                        name=rn, kind="reverse", is_relation=True, target=m.name,
                        origin=(f.inherited_from or m.name, f.name),
                        lineno=f.lineno, end_lineno=f.end_lineno,
                    )
                    tgt.reverse[rn] = rev
                    self.field_owners[rn].append(tgt)
                    self.accessors[rn].append(tgt)

    # ----------------------------------------------------------- resolution
    def resolve_path(self, root: ModelInfo, segs: list[str]) -> list[tuple[ModelInfo, FieldInfo]]:
        """Resolve a Django-style lookup path ('customer__address__city__icontains')."""
        steps: list[tuple[ModelInfo, FieldInfo]] = []
        cur: ModelInfo | None = root
        for seg in segs:
            if cur is None or not seg:
                break
            f = cur.lookup(seg)
            if f is None and seg.endswith("_id"):
                base = cur.fields.get(seg[:-3])
                if base is not None and base.is_relation:
                    steps.append((cur, base))
                break
            if f is None:
                break  # operator, transform or unknown name
            steps.append((cur, f))
            cur = self.get(f.target) if f.is_relation else None
        return steps

    def normalize(self, change: Change) -> tuple[Change, list[str]]:
        """Move a change on an inherited field to the defining class."""
        warnings: list[str] = []
        m = self.get(change.model)
        if m is None:
            warnings.append(f"model {change.model!r} not found")
            return change, warnings
        f = m.fields.get(change.field)
        if f is None and change.op != "add":
            warnings.append(f"field {change.model}.{change.field} not found")
        elif f is not None and f.inherited_from and f.inherited_from != m.name:
            warnings.append(
                f"{change.model}.{change.field} is defined in {f.inherited_from}; "
                f"analysing the change there (affects all subclasses)"
            )
            change = Change(change.op, f.inherited_from, change.field, change.new_name,
                            change.new_type, change.target, change.required)
        return change, warnings

    def models_with_field(self, name: str) -> list[ModelInfo]:
        return self.field_owners.get(name, [])


def affects(change: Change, model: ModelInfo, f: FieldInfo) -> bool:
    """Does an access to ``model.f`` refer to the attribute changed by ``change``?"""
    if f.origin is not None:
        return f.origin == (change.model, change.field)
    if f.name != change.field:
        return False
    return change.model in (model.name, f.inherited_from)


# =================================================================== build
def build_registry(project: Project) -> Registry:
    raws: dict[str, list[_RawClass]] = defaultdict(list)
    decl_base_names: set[str] = set(SA_DECL_BASES)
    sa_base_assign: set[str] = set()

    modules = list(project.modules(skip_migrations=True))
    for mod in modules:
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                cn = call_name(node.value)
                if cn in ("declarative_base", "generate_base", "as_declarative_base"):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            sa_base_assign.add(t.id)
                if cn == "SQLAlchemy":  # Flask-SQLAlchemy: db = SQLAlchemy() -> db.Model
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            sa_base_assign.add(f"{t.id}.Model")
            if isinstance(node, ast.ClassDef):
                bases = [dotted(b) or "" for b in node.bases]
                raws[node.name].append(_RawClass(node.name, node, mod, bases))

    # declarative bases: transitively, classes deriving from DeclarativeBase
    changed = True
    while changed:
        changed = False
        for name, lst in raws.items():
            for rc in lst:
                if rc.is_decl_base:
                    continue
                if any(b.split(".")[-1] in decl_base_names for b in rc.bases) and not _has_tablename(rc.node):
                    if not _sa_fields_present(rc.node):
                        rc.is_decl_base = True
                        decl_base_names.add(name)
                        changed = True

    sa_bases = decl_base_names | sa_base_assign

    def classify(rc: _RawClass) -> str | None:
        if rc.is_decl_base:
            return None
        mod = rc.module
        uses_django = "django" in mod.imports
        uses_sa = bool(SA_IMPORTS & mod.imports)
        bases_last = {b.split(".")[-1] for b in rc.bases}
        if uses_django and (set(rc.bases) & DJANGO_MODEL_BASES):
            return "django"
        if any(b in sa_bases or b.split(".")[-1] in decl_base_names for b in rc.bases):
            return "sqlalchemy"
        if any(b.endswith(".Model") for b in rc.bases) and uses_sa:
            return "sqlalchemy"
        if "SQLModel" in bases_last and any(
            kw.arg == "table" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in rc.node.keywords
        ):
            return "sqlalchemy"
        if uses_django and _django_fields_present(rc.node):
            return "django"
        if uses_sa and (_has_tablename(rc.node) or _sa_fields_present(rc.node)):
            return "sqlalchemy"
        return None

    for lst in raws.values():
        for rc in lst:
            rc.orm = classify(rc)

    # propagate through inheritance (subclasses of known models are models)
    changed = True
    while changed:
        changed = False
        for lst in raws.values():
            for rc in lst:
                if rc.orm is not None or rc.is_decl_base:
                    continue
                for b in rc.bases:
                    for parent in raws.get(b.split(".")[-1], []):
                        if parent.orm is not None:
                            rc.orm = parent.orm
                            changed = True
                            break
                    if rc.orm:
                        break

    # own fields
    for lst in raws.values():
        for rc in lst:
            if rc.orm == "django":
                _django_own(rc)
            elif rc.orm == "sqlalchemy":
                _sa_own(rc)

    # mixins without ORM classification but with SA columns (declared_attr etc.)
    for lst in raws.values():
        for rc in lst:
            if rc.orm is None and not rc.is_decl_base and (SA_IMPORTS & rc.module.imports) and _sa_fields_present(rc.node):
                rc.orm = "sqlalchemy"
                rc.abstract = True
                _sa_own(rc)
                rc.abstract = True

    models: list[ModelInfo] = []
    for lst in raws.values():
        for rc in lst:
            if rc.orm is None:
                continue
            fields = _inherited_fields(rc, raws, set())
            if not fields and not rc.table:
                continue
            node = rc.node
            last = max((s.end_lineno or s.lineno) for s in node.body) if node.body else node.lineno
            indent = node.body[0].col_offset if node.body else node.col_offset + 4
            table = rc.table
            if table is None and not rc.abstract:
                if rc.orm == "django":
                    table = f"{rc.app_label}_{rc.name.lower()}" if rc.app_label else rc.name.lower()
                else:
                    table = _snake(rc.name)
            models.append(
                ModelInfo(
                    name=rc.name, orm=rc.orm, path=rc.module.rel, lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno, table=table,
                    app_label=rc.app_label, abstract=rc.abstract, bases=rc.bases,
                    fields=fields, body_indent=indent, last_body_line=last,
                )
            )
    models.sort(key=lambda m: (m.path, m.lineno))
    return Registry(models)


def _inherited_fields(rc: _RawClass, raws: dict[str, list[_RawClass]], seen: set[str]) -> dict[str, FieldInfo]:
    if rc.name in seen:
        return dict(rc.fields)
    seen = seen | {rc.name}
    merged: dict[str, FieldInfo] = {}
    for b in rc.bases:
        for parent in raws.get(b.split(".")[-1], []):
            if parent is rc or parent.orm is None:
                continue
            for name, f in _inherited_fields(parent, raws, seen).items():
                if name not in merged:
                    g = FieldInfo(**{**f.__dict__})
                    g.inherited_from = f.inherited_from or parent.name
                    merged[name] = g
            break
    merged.update(rc.fields)
    return merged


# ----------------------------------------------------------- Django fields
def _django_field_call(value: ast.expr) -> str | None:
    if not isinstance(value, ast.Call):
        return None
    full = dotted(value.func) or ""
    if full.startswith(NOT_DJANGO_PREFIXES):
        return None
    name = last_name(value.func)
    if not name:
        return None
    if name in DJANGO_RELATIONS or name in DJANGO_OTHER_FIELDS or name.endswith("Field"):
        return name
    return None


def _django_fields_present(node: ast.ClassDef) -> bool:
    for _, value, _ in class_assigns(node):
        if isinstance(value, ast.Call) and (dotted(value.func) or "").startswith("models.") and _django_field_call(value):
            return True
    return False


def _django_type(kind: str, value: ast.Call) -> str:
    parts = []
    for k in ("max_length", "max_digits", "decimal_places"):
        v = kwarg(value, k)
        if isinstance(v, ast.Constant):
            parts.append(f"{k}={v.value}")
    return f"{kind}({', '.join(parts)})" if parts else kind


def _django_own(rc: _RawClass) -> None:
    node, mod = rc.node, rc.module
    rc.app_label = _app_label(mod.rel)
    meta = inner_class(node, "Meta")
    if meta is not None:
        for name, value, _ in class_assigns(meta):
            if name == "db_table" and const_str(value):
                rc.table = const_str(value)
            elif name == "abstract" and isinstance(value, ast.Constant) and value.value is True:
                rc.abstract = True
            elif name == "app_label" and const_str(value):
                rc.app_label = const_str(value)
    for name, value, stmt in class_assigns(node):
        kind = _django_field_call(value)
        if kind is None or name.startswith("_") or name == "objects":
            continue
        assert isinstance(value, ast.Call)
        f = FieldInfo(
            name=name, kind=kind, lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
            col_offset=stmt.col_offset, end_col_offset=stmt.end_col_offset or 0,
            name_col=stmt.col_offset, value_span=span(value), signature=ast.unparse(value),
        )
        if isinstance(stmt, ast.AnnAssign):
            f.annotation_span = span(stmt.annotation)
        f.nullable = _bool_kw(value, "null")
        f.has_default = kwarg(value, "default") is not None or kind in ("AutoField", "BigAutoField", "SmallAutoField") \
            or _bool_kw(value, "auto_now") is True or _bool_kw(value, "auto_now_add") is True \
            or (kind in DJANGO_STRING_FIELDS and f.nullable is not True) \
            or kind in ("ManyToManyField", "GenericRelation", "GenericForeignKey")
        f.primary_key = _bool_kw(value, "primary_key") is True
        f.db_nullable = f.nullable is True
        f.db_default = kwarg(value, "db_default") is not None or kind in ("AutoField", "BigAutoField", "SmallAutoField")
        f.unique = _bool_kw(value, "unique") is True or f.primary_key or kind in ("OneToOneField",)
        f.col_type = _django_type(kind, value)
        rel = DJANGO_RELATIONS.get(kind)
        if rel:
            f.is_relation = True
            tgt = value.args[0] if value.args else kwarg(value, "to")
            f.target = _target_name(tgt, rc.name)
            rn = const_str(kwarg(value, "related_name"))
            rq = const_str(kwarg(value, "related_query_name"))
            if rel == "o2o":
                f.reverse_name = rn or rc.name.lower()
            else:
                f.reverse_name = rn or f"{rc.name.lower()}_set"
            f.reverse_query_name = rq or rn or rc.name.lower()
            if rel != "m2m":
                f.column = const_str(kwarg(value, "db_column")) or f"{name}_id"
            else:
                f.m2m_table = const_str(kwarg(value, "db_table"))  # resolved against the model table later
        elif kind in DJANGO_OTHER_FIELDS:
            f.is_relation = kind == "GenericRelation"
            f.target = _target_name(value.args[0], rc.name) if value.args and kind == "GenericRelation" else None
        else:
            f.column = const_str(kwarg(value, "db_column")) or name
        rc.fields[name] = f


# ------------------------------------------------------- SQLAlchemy fields
def _has_tablename(node: ast.ClassDef) -> bool:
    return any(name in ("__tablename__", "__table__") for name, _, _ in class_assigns(node))


def _mapped_ann(ann: ast.AST | None) -> bool:
    if isinstance(ann, ast.Subscript):
        return last_name(ann.value) in SA_MAPPED
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return ann.value.split("[")[0].split(".")[-1] in SA_MAPPED
    return False


def _sa_fields_present(node: ast.ClassDef) -> bool:
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and _mapped_ann(stmt.annotation):
            return True
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Call):
            if call_name(stmt.value) in SA_COLUMN_CALLS | SA_RELATION_CALLS:
                return True
    return False


def _ann_target(ann: ast.AST | None) -> str | None:
    """Mapped[List["Address"]] -> Address."""
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.Subscript):
        inner = ann.slice
        if isinstance(inner, ast.Tuple) and inner.elts:
            inner = inner.elts[0]
        return _ann_target(inner)
    if isinstance(ann, ast.BinOp):  # "X | None"
        return _ann_target(ann.left)
    s = const_str(ann)
    if s:
        return s.split(".")[-1]
    n = last_name(ann)
    if n and n not in ("None", "Optional", "List", "list", "Set", "set"):
        return n
    return None


def _ann_optional(ann: ast.AST | None) -> bool:
    if ann is None:
        return False
    text = ast.unparse(ann)
    return "Optional[" in text or "| None" in text or "None |" in text


def _sa_own(rc: _RawClass) -> None:
    node, mod = rc.node, rc.module
    is_sqlmodel = any(b.split(".")[-1] == "SQLModel" for b in rc.bases)
    for name, value, _ in class_assigns(node):
        if name == "__tablename__" and const_str(value):
            rc.table = const_str(value)
        if name == "__abstract__" and isinstance(value, ast.Constant) and value.value is True:
            rc.abstract = True
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name, ann, value = stmt.target.id, stmt.annotation, stmt.value
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, ann, value = stmt.targets[0].id, None, stmt.value
        else:
            continue
        if name.startswith("__"):
            continue
        cn = call_name(value) if value is not None else None
        mapped = _mapped_ann(ann)
        if not (mapped or cn in SA_COLUMN_CALLS or cn in SA_RELATION_CALLS or (is_sqlmodel and ann is not None)):
            continue
        if is_sqlmodel and cn not in SA_RELATION_CALLS and cn not in SA_COLUMN_CALLS and cn not in (None, "Field"):
            continue
        f = FieldInfo(
            name=name, kind=cn or ("Mapped" if mapped else "annotation"),
            lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
            col_offset=stmt.col_offset, end_col_offset=stmt.end_col_offset or 0,
            name_col=stmt.col_offset,
            annotation_span=span(ann) if ann is not None else None,
            value_span=span(value) if value is not None else None,
            signature=(ast.unparse(ann) if ann is not None else "") + " = " + (ast.unparse(value) if value is not None else ""),
        )
        if cn in SA_RELATION_CALLS:
            f.is_relation = True
            tgt = value.args[0] if isinstance(value, ast.Call) and value.args else None
            f.target = _target_name(tgt, rc.name) or _ann_target(ann)
            bp = kwarg(value, "back_populates") if isinstance(value, ast.Call) else None
            br = kwarg(value, "backref") if isinstance(value, ast.Call) else None
            if br is not None:
                f.reverse_name = const_str(br) or (const_str(br.args[0]) if isinstance(br, ast.Call) and br.args else None)
            elif const_str(bp):
                f.reverse_query_name = None  # declared explicitly on the other side
            f.has_default = True
        else:
            col = name
            if isinstance(value, ast.Call):
                if value.args and const_str(value.args[0]):
                    col = const_str(value.args[0]) or name
                for a in value.args:
                    if isinstance(a, ast.Call) and call_name(a) == "ForeignKey" and a.args:
                        f.fk = const_str(a.args[0])
                nb = _bool_kw(value, "nullable")
                f.nullable = nb if nb is not None else (_ann_optional(ann) or None)
                f.primary_key = _bool_kw(value, "primary_key") is True
                if nb is not None:
                    f.db_nullable = nb
                elif cn == "Column":
                    f.db_nullable = not f.primary_key
                else:
                    f.db_nullable = _ann_optional(ann) and not f.primary_key
                f.db_default = kwarg(value, "server_default") is not None or (
                    f.primary_key and _bool_kw(value, "autoincrement") is not False)
                f.unique = _bool_kw(value, "unique") is True or f.primary_key
                f.col_type = next((ast.unparse(a) for a in value.args
                                   if not const_str(a) and call_name(a) != "ForeignKey"), "") or (
                    ast.unparse(ann) if ann is not None else "")
                f.has_default = any(kwarg(value, k) is not None for k in ("default", "server_default", "default_factory", "insert_default")) \
                    or _bool_kw(value, "primary_key") is True or _bool_kw(value, "autoincrement") is True
                if cn in ("column_property", "query_expression", "hybrid_property", "association_proxy", "synonym"):
                    col = None
                    f.has_default = True
            else:
                f.nullable = _ann_optional(ann) or None
                f.db_nullable = _ann_optional(ann)
                f.col_type = ast.unparse(ann) if ann is not None else ""
            f.column = col
        rc.fields[name] = f
