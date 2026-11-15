"""Derive model changes by comparing two registries (before / after a commit)."""

from __future__ import annotations

import re

from .core import Change, FieldInfo, ModelInfo
from .registry import Registry


def _own(m: ModelInfo) -> dict[str, FieldInfo]:
    return {n: f for n, f in m.fields.items() if not f.inherited_from or f.inherited_from == m.name}


def _type_of(f: FieldInfo) -> str:
    """The part of a declaration that determines the attribute's type."""
    sig = f.signature
    if f.kind in ("Mapped", "mapped_column", "Column", "relationship", "annotation") or "Mapped[" in sig:
        ann = sig.split(" = ")[0]
        if ann:
            return re.sub(r"\s+", "", ann)
        m = re.match(r"\w+\(([^,)]*)", sig.split(" = ", 1)[-1])
        return f"{f.kind}:{m.group(1) if m else ''}"
    return f"{f.kind}->{f.target}" if f.target else f.kind


def _same_shape(a: FieldInfo, b: FieldInfo) -> bool:
    def strip(sig: str, name: str) -> str:
        return re.sub(rf"\b{re.escape(name)}\b", "§", sig)
    return a.kind == b.kind and strip(a.signature, a.name) == strip(b.signature, b.name)


def diff_registries(before: Registry, after: Registry, only_paths: set[str] | None = None) -> tuple[list[Change], list[str]]:
    changes: list[Change] = []
    notes: list[str] = []
    after_names = {m.name for m in after.models}
    before_names = {m.name for m in before.models}
    new_models = [m for m in after.models if m.name not in before_names]
    for bm in before.models:
        if only_paths is not None and bm.path not in only_paths:
            continue
        am = after.get(bm.name) if bm.name in after_names else None
        own_b = _own(bm)
        if am is None:
            notes.append(f"model {bm.name} removed")
            for name in own_b:
                changes.append(Change("remove", bm.name, name))
            continue
        own_a = _own(am)
        removed = [n for n in own_b if n not in own_a]
        added = [n for n in own_a if n not in own_b]
        # renames: one removed and one added field with the same declaration shape
        renames: dict[str, str] = {}
        for r in removed:
            cands = [a for a in added if a not in renames.values() and _same_shape(own_b[r], own_a[a])]
            if len(cands) == 1:
                renames[r] = cands[0]
        # extract entity: removed fields reappear in a new model referenced from this one
        extracted: dict[str, str] = {}
        for nm in new_models:
            refs = [f for f in own_a.values() if f.is_relation and f.target == nm.name] or [
                f for f in nm.fields.values() if f.is_relation and f.target == bm.name]
            if not refs:
                continue
            for r in removed:
                if r not in renames and r in nm.fields:
                    extracted[r] = nm.name
        for r in removed:
            if r in renames:
                changes.append(Change("rename", bm.name, r, new_name=renames[r]))
            elif r in extracted:
                changes.append(Change("extract", bm.name, r, target=extracted[r]))
            else:
                changes.append(Change("remove", bm.name, r))
        for a in added:
            if a in renames.values():
                continue
            f = own_a[a]
            if f.is_relation and f.target in {nm.name for nm in new_models} and extracted:
                notes.append(f"{bm.name}.{a} added as the reference to the extracted entity {f.target}")
            changes.append(Change("add", bm.name, a, new_type=f.kind, required=f.required))
        for n in own_b:
            if n in own_a:
                fb, fa = own_b[n], own_a[n]
                if _type_of(fb) != _type_of(fa):
                    changes.append(Change("retype", bm.name, n, new_type=_type_of(fa)))
                elif fb.signature != fa.signature:
                    notes.append(f"{bm.name}.{n}: options changed ({fb.signature!r} -> {fa.signature!r})")
                elif fb.column != fa.column:
                    changes.append(Change("retype", bm.name, n, new_type=f"column {fa.column}"))
    for nm in new_models:
        notes.append(f"model {nm.name} added")
    return changes, notes
