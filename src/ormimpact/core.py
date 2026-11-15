"""Core data types: models, fields, changes and findings."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# Layers, in the order of the thesis' extension (L1..L4) plus baselines.
LAYER_CHECKER = "checker"      # L1: new type-checker errors
LAYER_ORM = "orm"              # L2: ORM lookups and query APIs (strings, kwargs)
LAYER_CONFIG = "config"        # L2: serializers, forms, admin, factories, Meta options
LAYER_DYNAMIC = "dynamic"      # L2: getattr/setattr, constructor kwargs, update_fields
LAYER_SQL = "sql"              # L3: raw SQL in code and .sql files
LAYER_DATA = "data"            # L3: fixtures and data files
LAYER_TEMPLATE = "template"    # L3: templates
LAYER_HISTORY = "history"      # L4: co-change mining
LAYER_TEXT = "text"            # baseline: plain identifier search

ALL_LAYERS = [
    LAYER_CHECKER,
    LAYER_ORM,
    LAYER_CONFIG,
    LAYER_DYNAMIC,
    LAYER_SQL,
    LAYER_DATA,
    LAYER_TEMPLATE,
    LAYER_HISTORY,
    LAYER_TEXT,
]
DEFAULT_LAYERS = [l for l in ALL_LAYERS if l != LAYER_TEXT]

CHANGE_OPS = ("add", "remove", "rename", "retype", "extract")


@dataclass
class FieldInfo:
    """A mapped attribute of a model (column, relation or reverse relation)."""

    name: str
    kind: str                                   # e.g. CharField, ForeignKey, Mapped, relationship
    column: str | None = None                   # database column, if any
    is_relation: bool = False
    target: str | None = None                   # related model name
    reverse_name: str | None = None             # accessor created on the target model
    reverse_query_name: str | None = None       # Django: name used in lookups on the target
    origin: tuple[str, str] | None = None       # for reverse relations: defining (model, field)
    fk: str | None = None                       # SQLAlchemy ForeignKey("table.col")
    nullable: bool | None = None
    has_default: bool = False
    lineno: int = 0
    end_lineno: int = 0
    col_offset: int = 0
    end_col_offset: int = 0
    name_col: int = 0
    annotation_span: tuple[int, int, int, int] | None = None
    value_span: tuple[int, int, int, int] | None = None
    signature: str = ""                         # normalised declaration (for diffing)
    inherited_from: str | None = None           # class that defines the field
    # database-level properties (for schema snapshots and cross-version checks)
    db_nullable: bool = True
    db_default: bool = False                    # default enforced by the database (db_default / server_default / serial)
    col_type: str = ""
    unique: bool = False
    primary_key: bool = False
    m2m_table: str | None = None                # Django ManyToManyField join table

    @property
    def required(self) -> bool:
        """Must be given when an instance is created (no default, not nullable)."""
        if self.kind in ("reverse", "relationship", "Relationship"):
            return False
        return not self.has_default and self.nullable is not True


@dataclass
class ModelInfo:
    name: str
    orm: str                                    # "django" | "sqlalchemy"
    path: str                                   # posix path relative to the project root
    lineno: int
    end_lineno: int
    table: str | None = None
    app_label: str | None = None
    abstract: bool = False
    bases: list[str] = field(default_factory=list)
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    reverse: dict[str, FieldInfo] = field(default_factory=dict)
    body_indent: int = 4
    last_body_line: int = 0

    @property
    def label(self) -> str:
        if self.app_label:
            return f"{self.app_label}.{self.name.lower()}"
        return self.name.lower()

    def lookup(self, name: str) -> FieldInfo | None:
        return self.fields.get(name) or self.reverse.get(name)

    def by_column(self, column: str) -> FieldInfo | None:
        for f in self.fields.values():
            if f.column == column:
                return f
        return None


@dataclass(frozen=True)
class Change:
    """A domain-model change.

    op: add | remove | rename | retype | extract
    extract means the field moves to a new entity ``target`` (Extract Entity);
    for the old model this behaves like a removal of the attribute.
    """

    op: str
    model: str
    field: str
    new_name: str | None = None
    new_type: str | None = None
    target: str | None = None
    required: bool | None = None

    def describe(self) -> str:
        base = f"{self.op} {self.model}.{self.field}"
        if self.op == "rename" and self.new_name:
            return f"{base} -> {self.new_name}"
        if self.op == "retype" and self.new_type:
            return f"{base} : {self.new_type}"
        if self.op == "extract" and self.target:
            return f"{base} -> {self.target}"
        if self.op == "add" and self.new_type:
            return f"{base} : {self.new_type}"
        return base

    @property
    def breaks_name(self) -> bool:
        """True if references to the old attribute name stop working."""
        return self.op in ("remove", "rename", "extract")

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        d["description"] = self.describe()
        return d


_SPEC_RE = re.compile(
    r"^\s*(?P<op>add|remove|rename|retype|extract)\s+"
    r"(?P<model>[A-Za-z_]\w*)\.(?P<fields>[A-Za-z_][\w,.]*?)"
    r"(?:\s*(?:->|=>|:|\s)\s*(?P<arg>.+?))?\s*$"
)


def parse_change(spec: str) -> list[Change]:
    """Parse a change specification.

    Examples::

        remove Order.total
        rename Order.total -> amount
        retype Order.total : Mapped[Decimal]
        add Order.discount : Mapped[int]
        extract Order.delivery_address,city -> Address
    """
    m = _SPEC_RE.match(spec)
    if not m:
        raise ValueError(
            f"cannot parse change {spec!r}; expected e.g. 'rename Order.total -> amount'"
        )
    op, model, fields_s, arg = m.group("op"), m.group("model"), m.group("fields"), m.group("arg")
    names = []
    for part in fields_s.replace(" ", "").split(","):
        if not part:
            continue
        if "." in part:  # allow "Order.a,Order.b"
            mdl, _, part = part.partition(".")
            if mdl != model:
                raise ValueError(f"all fields in {spec!r} must belong to {model}")
        names.append(part)
    if not names:
        raise ValueError(f"no field in change {spec!r}")
    if op != "extract" and len(names) > 1:
        raise ValueError(f"only 'extract' accepts several fields: {spec!r}")
    if op == "rename" and not arg:
        raise ValueError(f"rename needs a new name: {spec!r}")
    out = []
    for name in names:
        if op == "rename":
            out.append(Change(op, model, name, new_name=arg.strip()))
        elif op == "extract":
            out.append(Change(op, model, name, target=(arg or "").strip() or None))
        elif op in ("retype", "add"):
            out.append(Change(op, model, name, new_type=(arg or "").strip() or None))
        else:
            out.append(Change(op, model, name))
    return out


@dataclass
class Finding:
    path: str
    line: int
    layer: str
    kind: str
    confidence: str
    change: str
    evidence: str
    col: int = 0
    symbol: str | None = None
    detail: str | None = None

    def key(self) -> tuple:
        return (self.path, self.line, self.layer, self.kind, self.change)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @property
    def rank(self) -> int:
        return CONFIDENCE_ORDER.get(self.confidence, 0)


def at_least(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_ORDER.get(confidence, 0) >= CONFIDENCE_ORDER.get(minimum, 0)
