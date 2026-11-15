"""Baseline: plain identifier search (layer: text). Off by default.

Every occurrence of a changed attribute name in code, templates, SQL and data
files. This is what a developer gets from grep; it is used as a baseline in
the evaluation.
"""

from __future__ import annotations

import re

from ..core import LAYER_TEXT
from ..project import DATA_SUFFIXES, SQL_SUFFIXES, TEMPLATE_SUFFIXES
from .base import Context


def run(ctx: Context) -> None:
    patterns = []
    for c in ctx.changes:
        if c.op == "add":
            continue
        for n in ctx.change_names(c):
            if len(n) >= 2:
                patterns.append((re.compile(rf"\b{re.escape(n)}\b"), c, n))
    if not patterns:
        return
    suffixes = {".py"} | TEMPLATE_SUFFIXES | SQL_SUFFIXES | DATA_SUFFIXES
    for path in ctx.project.files(suffixes):
        text = ctx.project.read_text(path)
        if not text:
            continue
        rel = ctx.project.rel(path)
        for i, line in enumerate(text.splitlines(), 1):
            for rx, c, n in patterns:
                if n in line and rx.search(line):
                    ctx.add(rel, i, LAYER_TEXT, "identifier", "low", c, evidence=line.strip()[:160])
