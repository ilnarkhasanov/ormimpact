"""Co-change mining from version history (layer: history).

Files that changed together with a model's file in past commits are reported,
ranked by confidence = co-changes / changes of the model file (Zimmermann et al.,
TSE 2005).
"""

from __future__ import annotations

from collections import Counter

from ..core import LAYER_HISTORY
from ..gitutil import GitError, log_files, toplevel
from ..project import is_migration
from .base import Context

# Cache of history per (repo, rev, n)
_CACHE: dict[tuple[str, str, int], list[tuple[str, list[str]]]] = {}


def run(ctx: Context) -> None:
    root = ctx.project.root
    top = toplevel(root)
    if top is None:
        ctx.warnings.append("history layer skipped: the project is not inside a git repository")
        return
    try:
        prefix = root.relative_to(top).as_posix()
    except ValueError:
        return
    prefix = "" if prefix == "." else prefix + "/"
    rev = ctx.options.history_rev or "HEAD"
    key = (str(top), rev, ctx.options.history_commits)
    try:
        if key not in _CACHE:
            _CACHE[key] = log_files(top, rev, ctx.options.history_commits)
        commits = _CACHE[key]
    except GitError as e:
        msg = str(e).splitlines()[0]
        if "unknown revision" in str(e) or "does not have any commits" in str(e):
            msg = f"no commits at {rev}"
        ctx.warnings.append(f"history layer skipped: {msg}")
        return
    run_on_commits(ctx, commits, prefix)


def run_on_commits(ctx: Context, commits: list[tuple[str, list[str]]], prefix: str = "") -> None:
    files_by_commit = []
    for _, paths in commits:
        rel = [p[len(prefix):] for p in paths if p.startswith(prefix)]
        if 0 < len(rel) <= ctx.options.history_max_files:
            files_by_commit.append(set(rel))
    by_model_file: dict[str, list] = {}
    for c in ctx.changes:
        m = ctx.registry.get(c.model)
        if m is not None:
            by_model_file.setdefault(m.path, []).append(c)
    for mfile, changes in by_model_file.items():
        occ = 0
        co: Counter[str] = Counter()
        for files in files_by_commit:
            if mfile in files:
                occ += 1
                co.update(f for f in files if f != mfile)
        if occ == 0:
            continue
        for f, n in co.most_common():
            conf = n / occ
            if n < ctx.options.history_min_support or conf < ctx.options.history_min_confidence:
                continue
            if is_migration(f) or not ctx.rel_exists(f):
                continue
            level = "medium" if conf >= 0.5 else "low"
            for c in changes:
                ctx.add(f, 0, LAYER_HISTORY, "co-change", level, c,
                        evidence=f"changed together with {mfile} in {n} of {occ} commits ({conf:.0%})",
                        detail=f"support={n} confidence={conf:.2f}")
