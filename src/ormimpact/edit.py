"""Apply a change specification to model source code (for the checker run)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core import Change, ModelInfo
from .registry import Registry


@dataclass
class Edit:
    start_line: int     # 1-based
    start_col: int      # character offset
    end_line: int
    end_col: int
    text: str


def _char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def plan_edits(registry: Registry, changes: list[Change], sources: dict[str, str]) -> tuple[dict[str, list[Edit]], list[str]]:
    edits: dict[str, list[Edit]] = {}
    warnings: list[str] = []
    for c in changes:
        m = registry.get(c.model)
        if m is None:
            warnings.append(f"checker: model {c.model} not found, change not applied")
            continue
        src = sources.get(m.path)
        if src is None:
            continue
        lines = src.splitlines(keepends=True)
        if c.op == "add":
            edits.setdefault(m.path, []).append(_add(m, c, lines))
            continue
        f = m.fields.get(c.field)
        if f is None or (f.inherited_from and f.inherited_from != m.name):
            warnings.append(f"checker: {c.model}.{c.field} not declared in {c.model}; change not applied")
            continue
        if c.op in ("remove", "extract"):
            edits.setdefault(m.path, []).append(Edit(f.lineno, 0, f.end_lineno + 1, 0, ""))
        elif c.op == "rename":
            line = lines[f.lineno - 1]
            col = _char_col(line, f.name_col)
            edits.setdefault(m.path, []).append(Edit(f.lineno, col, f.lineno, col + len(f.name), c.new_name or f.name))
        elif c.op == "retype":
            e = _retype(m, f, c, lines)
            if e is None:
                warnings.append(f"checker: retype {c.model}.{c.field} needs a new type (e.g. ': Mapped[Decimal]' or "
                                f"': models.DecimalField(max_digits=10, decimal_places=2)'); change not applied")
            else:
                edits.setdefault(m.path, []).append(e)
    return edits, warnings


def _span_edit(lines: list[str], span: tuple[int, int, int, int], text: str) -> Edit:
    l1, c1, l2, c2 = span
    return Edit(l1, _char_col(lines[l1 - 1], c1), l2, _char_col(lines[l2 - 1], c2), text)


def _retype(m: ModelInfo, f, c: Change, lines: list[str]) -> Edit | None:
    t = (c.new_type or "").strip()
    if not t:
        return None
    if m.orm == "sqlalchemy":
        if f.annotation_span:
            ann = t if re.match(r"^(Mapped|WriteOnlyMapped|DynamicMapped)\[", t) else f"Mapped[{t}]"
            return _span_edit(lines, f.annotation_span, ann)
        if f.value_span:
            return _span_edit(lines, f.value_span, t)
        return None
    if f.value_span is None:
        return None
    if "(" in t:
        return _span_edit(lines, f.value_span, t)
    # bare field class: models.IntegerField(default=0) -> models.DecimalField(default=0)
    return _span_edit(lines, f.value_span, _replace_value(lines, f.value_span, f.kind, t))


def _replace_value(lines, span, kind, new_kind) -> str:
    l1, c1, l2, c2 = span
    first = lines[l1 - 1]
    last = lines[l2 - 1]
    s = _char_col(first, c1)
    e = _char_col(last, c2)
    if l1 == l2:
        seg = first[s:e]
    else:
        seg = first[s:] + "".join(lines[l1:l2 - 1]) + last[:e]
    return re.sub(rf"\b{re.escape(kind)}\b", new_kind, seg, count=1)


def _add(m: ModelInfo, c: Change, lines: list[str]) -> Edit:
    indent = " " * m.body_indent
    t = (c.new_type or "").strip()
    if m.orm == "sqlalchemy":
        column_style = any(f.kind == "Column" and f.annotation_span is None for f in m.fields.values())
        if column_style:
            decl = f"{c.field} = Column({t or 'Integer'})"
        else:
            inner = t[len("Mapped["):-1] if t.startswith("Mapped[") else (t or "int")
            decl = f"{c.field}: Mapped[{inner}] = mapped_column()"
    else:
        if "(" in t:
            decl = f"{c.field} = {t}"
        else:
            decl = f"{c.field} = models.{t or 'IntegerField'}()"
    line = m.last_body_line + 1
    return Edit(line, 0, line, 0, f"{indent}{decl}\n")


def apply_edits(text: str, edits: list[Edit]) -> str:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    offsets = [0]
    for l in lines:
        offsets.append(offsets[-1] + len(l))
    flat = "".join(lines)

    def pos(line: int, col: int) -> int:
        if line - 1 >= len(lines):
            return len(flat)
        return offsets[line - 1] + col

    for e in sorted(edits, key=lambda e: (e.start_line, e.start_col), reverse=True):
        a, b = pos(e.start_line, e.start_col), pos(e.end_line, e.end_col)
        flat = flat[:a] + e.text + flat[b:]
    return flat


def apply_to_tree(root: Path, edits: dict[str, list[Edit]]) -> set[str]:
    done = set()
    for rel, es in edits.items():
        p = root / rel
        text = p.read_text(encoding="utf-8")
        p.write_text(apply_edits(text, es), encoding="utf-8")
        done.add(rel)
    return done
