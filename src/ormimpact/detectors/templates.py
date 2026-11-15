"""Django and Jinja templates (layer: template)."""

from __future__ import annotations

import re

from ..core import LAYER_TEMPLATE
from ..project import TEMPLATE_SUFFIXES
from .base import Context
from .dynamic import var_model_map

TAG_RE = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.DOTALL)
CHAIN_RE = re.compile(r"\b([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)+)")
STRING_RE = re.compile(r"""["']([A-Za-z_][\w.]*)["']""")


def run(ctx: Context) -> None:
    if not len(ctx.registry):
        return
    varmap = var_model_map(ctx)
    names: dict[str, list] = {}
    for c in ctx.changes:
        if c.op != "add":
            for n in ctx.change_names(c):
                names.setdefault(n, []).append(c)
    if not names:
        return
    for path in ctx.project.files(TEMPLATE_SUFFIXES):
        rel = ctx.project.rel(path)
        if path.suffix.lower() == ".txt" and "templates" not in rel.split("/"):
            continue
        if path.suffix.lower() == ".xml" and "templates" not in rel.split("/"):
            continue
        text = ctx.project.read_text(path)
        if not text or ("{{" not in text and "{%" not in text):
            continue
        for tag in TAG_RE.finditer(text):
            body = tag.group(1) if tag.group(1) is not None else tag.group(2)
            start = tag.start(1) if tag.group(1) is not None else tag.start(2)
            for m in CHAIN_RE.finditer(body):
                var = m.group(1)
                attrs = m.group(2).lstrip(".").split(".")
                if not any(a in names for a in attrs):
                    continue
                root = varmap.get(var) or varmap.get(var.rstrip("s"))
                if root is not None:
                    matches = ctx.by_path(root, "__".join(attrs))
                else:
                    matches = [mc for a in attrs if a in names for mc in ctx.by_name(a, "low")]
                if True:
                    for c, conf in matches:
                        # variable names are a convention, not a type; measured precision is low
                        conf = "low"
                        line = text.count("\n", 0, start + m.start()) + 1
                        ctx.add(rel, line, LAYER_TEMPLATE, "template-attribute", conf, c,
                                evidence=" ".join(tag.group(0).split())[:160],
                                detail=f"{var}.{'.'.join(attrs)}")
            for m in STRING_RE.finditer(body):  # {% regroup ... by "total" %}, |dictsort:"total"
                s = m.group(1)
                if s in names:
                    for c, conf in ctx.by_name(s, "low"):
                        line = text.count("\n", 0, start + m.start()) + 1
                        ctx.add(rel, line, LAYER_TEMPLATE, "template-string", "low", c,
                                evidence=" ".join(tag.group(0).split())[:160])
