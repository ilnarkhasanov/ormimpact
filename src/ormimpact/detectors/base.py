"""Shared context and name-resolution helpers for detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import Change, Finding, ModelInfo
from ..project import ParsedModule, Project
from ..registry import Registry, affects

DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


@dataclass
class Options:
    layers: list[str]
    history_commits: int = 1000
    history_rev: str | None = None
    history_min_support: int = 2
    history_min_confidence: float = 0.3
    history_max_files: int = 40
    sql_parser: bool = True


@dataclass
class Context:
    project: Project
    registry: Registry
    changes: list[Change]
    options: Options
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _names: dict[Change, set[str]] | None = None

    # ------------------------------------------------------------- output
    def add(
        self,
        where: ParsedModule | str,
        line: int,
        layer: str,
        kind: str,
        confidence: str,
        change: Change,
        evidence: str | None = None,
        col: int = 0,
        detail: str | None = None,
    ) -> None:
        if isinstance(where, ParsedModule):
            rel, symbol = where.rel, where.symbol_at(line) if line else None
            ev = evidence if evidence is not None else where.snippet(line)
        else:
            rel, symbol, ev = where, None, evidence or ""
        self.findings.append(
            Finding(path=rel, line=line, col=col, layer=layer, kind=kind, confidence=confidence,
                    change=change.describe(), evidence=ev, symbol=symbol, detail=detail)
        )

    # --------------------------------------------------------- resolution
    def change_names(self, c: Change) -> set[str]:
        """Attribute names whose use refers to the changed attribute."""
        if self._names is None:
            self._names = {}
        if c not in self._names:
            names = {c.field}
            m = self.registry.get(c.model)
            f = m.fields.get(c.field) if m else None
            if f is not None and f.is_relation and f.target:
                tgt = self.registry.get(f.target)
                if tgt is not None:
                    for rn, rf in tgt.reverse.items():
                        if rf.origin == (c.model, c.field):
                            names.add(rn)
            if f is not None and f.column and f.column != f.name:
                names.add(f.column)  # customer_id, or an explicit column name
            self._names[c] = names
        return self._names[c]

    def by_name(self, name: str, base: str = "medium") -> list[tuple[Change, str]]:
        """Match an attribute name without knowing its model."""
        out = []
        for c in self.changes:
            if c.op == "add":
                continue
            if name in self.change_names(c):
                owners = {o.name for o in self.registry.models_with_field(name)}
                conf = base if len(owners) <= 1 else DOWNGRADE[base]
                out.append((c, _for_op(c, conf)))
        return out

    def by_path(self, root: ModelInfo | None, path: str, sep: str = "__",
                unknown_base: str = "medium") -> list[tuple[Change, str]]:
        """Match a lookup path such as 'customer__name__icontains' on ``root``."""
        segs = [s for s in path.split(sep) if s]
        if not segs:
            return []
        if root is None:
            return self.by_name(segs[0], unknown_base)
        out = []
        for m, f in self.registry.resolve_path(root, segs):
            for c in self.changes:
                if c.op != "add" and affects(c, m, f):
                    out.append((c, _for_op(c, "high")))
        return out

    def by_attr(self, root: ModelInfo | None, name: str, unknown_base: str = "low") -> list[tuple[Change, str]]:
        return self.by_path(root, name, sep="\0", unknown_base=unknown_base)

    def model(self, name: str | None) -> ModelInfo | None:
        return self.registry.get(name)

    def adds_for(self, model: ModelInfo | None) -> list[Change]:
        if model is None:
            return []
        names = {model.name, *model.bases}
        return [c for c in self.changes if c.op == "add" and (c.model in names or c.model == model.name)]

    def rel_exists(self, rel: str) -> bool:
        return (self.project.root / rel).exists()


def _for_op(c: Change, conf: str) -> str:
    # a type change keeps the name valid; references are worth a review only
    return DOWNGRADE[conf] if c.op == "retype" else conf


def model_ref(ctx: Context, node) -> ModelInfo | None:
    """Model referenced by an expression: Order, models.Order, 'shop.Order'."""
    from ..astutil import const_str, last_name

    s = const_str(node)
    if s is not None:
        return ctx.registry.by_label.get(s.lower()) or ctx.registry.get(s.split(".")[-1])
    n = last_name(node)
    return ctx.registry.get(n) if n else None


def strip_prefix(s: str) -> str:
    return s.lstrip("-^=@?+").strip()


def text_line(text: str, pos: int, base: int = 1) -> int:
    return base + text.count("\n", 0, pos)


def path_exists(p: Path) -> bool:
    try:
        return p.exists()
    except OSError:
        return False
