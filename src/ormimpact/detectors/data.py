"""Fixtures and data files (layer: data).

* Django fixtures (JSON/YAML lists of {"model": "app.model", "fields": {...}})
* other JSON/YAML/CSV files that use a changed column as a key or header,
  when the file name points at the model or its table
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..core import LAYER_DATA, Change, ModelInfo
from ..project import DATA_SUFFIXES
from .base import Context

SKIP_FILES = {"package.json", "package-lock.json", "tsconfig.json", "composer.json", "pyrightconfig.json",
              ".pre-commit-config.yaml", "docker-compose.yml", "docker-compose.yaml", "mkdocs.yml"}
MAX_BYTES = 5_000_000


def run(ctx: Context) -> None:
    if not len(ctx.registry) or not ctx.changes:
        return
    for path in ctx.project.files(DATA_SUFFIXES):
        if path.name in SKIP_FILES or path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        text = ctx.project.read_text(path)
        if not text:
            continue
        rel = ctx.project.rel(path)
        suffix = path.suffix.lower()
        data = None
        if suffix == ".json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(text)
            except Exception:
                continue
        elif suffix == ".csv":
            _csv(ctx, rel, path, text)
            continue
        if _is_django_fixture(data):
            _django_fixture(ctx, rel, text, data)
        else:
            _generic(ctx, rel, path, text, data)


def _is_django_fixture(data) -> bool:
    return isinstance(data, list) and bool(data) and all(
        isinstance(x, dict) and "model" in x and "fields" in x for x in data[:20]
    )


def _line_of(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    pos = text.find(needle, start)
    if pos < 0:
        return 1, start
    return text.count("\n", 0, pos) + 1, pos


def _django_fixture(ctx: Context, rel: str, text: str, data: list) -> None:
    reported: set[tuple[Change, str]] = set()
    cursor = 0
    for obj in data:
        label = str(obj.get("model", "")).lower()
        model = ctx.registry.by_label.get(label) or ctx.registry.get(label.split(".")[-1].capitalize())
        if model is None:
            model = next((m for m in ctx.registry.models if m.name.lower() == label.split(".")[-1]), None)
        fields = obj.get("fields") or {}
        _, cursor = _line_of(text, f'"{obj.get("model")}"', cursor)
        if model is None or not isinstance(fields, dict):
            continue
        for key in fields:
            for c, conf in ctx.by_attr(model, key):
                if (c, key) in reported:
                    continue
                reported.add((c, key))
                line, _ = _line_of(text, f'"{key}"', cursor)
                ctx.add(rel, line, LAYER_DATA, "django-fixture", conf, c,
                        evidence=f'{obj.get("model")}: "{key}"', detail="fixture entry uses the field")
        for c in ctx.adds_for(model):
            if c.required is not False and c.field not in fields and (c, "+") not in reported:
                reported.add((c, "+"))
                line, _ = _line_of(text, f'"{obj.get("model")}"')
                ctx.add(rel, line, LAYER_DATA, "fixture-missing-new-field", "medium", c,
                        evidence=f'{obj.get("model")} entries', detail=f"fixture rows lack the new field {c.field!r}")


def _file_mentions(path: Path, model: ModelInfo) -> bool:
    stem = path.stem.lower()
    names = {model.name.lower(), (model.table or "").lower()}
    names |= {n + "s" for n in names if n}
    return any(n and n in stem for n in names)


def _keys(data, depth: int = 0) -> set[str]:
    out: set[str] = set()
    if depth > 6:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            out.add(str(k))
            out |= _keys(v, depth + 1)
    elif isinstance(data, list):
        for v in data[:200]:
            out |= _keys(v, depth + 1)
    return out


def _generic(ctx: Context, rel: str, path: Path, text: str, data) -> None:
    if data is None:
        return
    keys = _keys(data)
    for c in ctx.changes:
        if c.op == "add":
            continue
        model = ctx.registry.get(c.model)
        if model is None:
            continue
        for name in ctx.change_names(c):
            if name in keys:
                conf = "medium" if _file_mentions(path, model) else "low"
                line, _ = _line_of(text, f'"{name}"') if f'"{name}"' in text else _line_of(text, f"{name}:")
                ctx.add(rel, line, LAYER_DATA, "data-key", conf, c, evidence=f"key {name!r}",
                        detail=f"data file uses the key {name!r}")
                break


def _csv(ctx: Context, rel: str, path: Path, text: str) -> None:
    try:
        header = next(csv.reader(io.StringIO(text)))
    except (StopIteration, csv.Error):
        return
    cols = {h.strip().lower() for h in header}
    for c in ctx.changes:
        if c.op == "add":
            continue
        model = ctx.registry.get(c.model)
        if model is None:
            continue
        for name in ctx.change_names(c):
            if name.lower() in cols:
                conf = "medium" if _file_mentions(path, model) else "low"
                ctx.add(rel, 1, LAYER_DATA, "csv-header", conf, c, evidence=",".join(header)[:160])
                break
