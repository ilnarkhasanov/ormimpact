"""Analyse a release (before tree -> after tree) for version-skew incompatibilities."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..gitutil import changed_files, export_tree, git, toplevel
from ..project import Project
from ..registry import build_registry
from .rules import PAIR_A, PAIR_B, Verdict, Version, evaluate, linter_verdict, summarize
from .schema import DbOp, is_migration_file, mapping, release_ops


@dataclass
class SkewReport:
    before: str
    after: str
    migrations: list[str]
    ops: list[DbOp]
    verdicts: list[Verdict]
    summary: dict
    notes: list[str] = field(default_factory=list)

    def linter(self) -> list[tuple[DbOp, bool, str]]:
        return [(op, *linter_verdict(op)) for op in self.ops]

    def to_dict(self) -> dict:
        return {
            "before": self.before, "after": self.after, "migrations": self.migrations,
            "summary": self.summary, "notes": self.notes,
            "linter": [{"op": op.describe(), "flags": f, "rule": r} for op, f, r in self.linter()],
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


def new_migration_files(before_root: Path, after_root: Path) -> list[str]:
    before = Project(before_root, include_migrations=True)
    after = Project(after_root, include_migrations=True)
    old = {before.rel(p) for p in before.files({".py"}, skip_migrations=False)}
    out = []
    for p in after.files({".py"}, skip_migrations=False):
        rel = after.rel(p)
        if rel in old:
            continue
        text = after.read_text(p) or ""
        if is_migration_file(rel, text):
            out.append(rel)
    return sorted(out)


def analyze_trees(before_root: Path, after_root: Path, migration_files: list[str] | None = None) -> SkewReport:
    before_root, after_root = Path(before_root).resolve(), Path(after_root).resolve()
    pb, pa = Project(before_root), Project(after_root)
    rb, ra = build_registry(pb), build_registry(pa)
    files = migration_files if migration_files is not None else new_migration_files(before_root, after_root)
    pa_all = Project(after_root, include_migrations=True)
    ops, notes = release_ops(pa_all, files, rb, ra)
    vb = Version(pb, rb, mapping(rb))
    va = Version(pa, ra, mapping(ra))
    verdicts = evaluate([o for o in ops], vb, va)
    if not files:
        notes.append("no new migration files in this release")
    # models that changed columns without a migration
    for t, tm in va.tables.items():
        old = vb.tables.get(t)
        if old is None:
            continue
        added = set(tm.columns) - set(old.columns)
        removed = set(old.columns) - set(tm.columns)
        covered = {(o.table, o.column) for o in ops} | {(o.table, o.new_name) for o in ops if o.new_name}
        for c in sorted(added | removed):
            if (t, c) not in covered:
                notes.append(f"{t}.{c} is {'mapped' if c in added else 'no longer mapped'} by version N+1 but no "
                             f"new migration changes it (state-only change or a missing migration)")
    return SkewReport(str(before_root), str(after_root), files, ops, verdicts, summarize(verdicts), notes)


def analyze_git(repo: Path, base: str, head: str | None = None, subdir: str = "") -> SkewReport:
    """base..head (head=None: the working tree)."""
    repo = Path(repo).resolve()
    top = toplevel(repo) or repo
    with tempfile.TemporaryDirectory(prefix="ormimpact-skew-") as tmp:
        b = Path(tmp) / "before"
        export_tree(top, base, b)
        if head is None:
            a = top
        else:
            a = Path(tmp) / "after"
            export_tree(top, head, a)
        rb = b / subdir if subdir else b
        ra = a / subdir if subdir else a
        files = None
        if head is not None:
            prefix = subdir.strip("/") + "/" if subdir else ""
            files = []
            for st, p, _ in changed_files(top, base, head):
                if st == "A" and p.startswith(prefix) and p.endswith(".py"):
                    rel = p[len(prefix):]
                    text = (ra / rel).read_text(encoding="utf-8", errors="replace") if (ra / rel).exists() else ""
                    if is_migration_file(rel, text):
                        files.append(rel)
        rep = analyze_trees(rb, ra, files)
        rep.before, rep.after = base, head or "working tree"
        return rep


# ------------------------------------------------------------- output
def to_text(rep: SkewReport, verbose: bool = False) -> str:
    s = rep.summary
    lines = [f"version-skew check: {rep.before} -> {rep.after}",
             f"  migrations: {', '.join(rep.migrations) or 'none'}", ""]
    if not rep.ops:
        lines.append("No database operations in this release.")
    else:
        lines.append(f"  {PAIR_A:<26} {s['pair_a'].upper()}")
        lines.append(f"  {PAIR_B:<26} {s['pair_b'].upper()}")
        lines.append("")
        for order, st in s["orders"].items():
            lines.append(f"  {st:<7} {order}")
        lines.append(f"  recommendation: {s['recommendation']}")
        lines.append("")
        for op in rep.ops:
            flag, rule = linter_verdict(op)
            lines.append(f"* {op.describe()}   [{op.path}:{op.line}]" + (f"   linter: {rule}" if flag else "   linter: ok"))
            for v in rep.verdicts:
                if v.op is not op:
                    continue
                if v.status == "safe" and not verbose:
                    lines.append(f"    {v.pair}: safe")
                    continue
                lines.append(f"    {v.pair}: {v.status.upper()} ({v.confidence}) - {v.reason}")
                for e in v.evidence[: (12 if verbose else 4)]:
                    lines.append(f"        {e.path}:{e.line}  {e.text[:110]}")
                if len(v.evidence) > (12 if verbose else 4):
                    lines.append(f"        ... {len(v.evidence) - (12 if verbose else 4)} more")
                if v.fix and v.status != "safe":
                    lines.append(f"      fix: {v.fix}")
            lines.append("")
    for n in rep.notes:
        lines.append(f"note: {n}")
    return "\n".join(lines).rstrip() + "\n"


def to_json(rep: SkewReport) -> str:
    return json.dumps(rep.to_dict(), indent=2, ensure_ascii=False)


def to_markdown(rep: SkewReport) -> str:
    s = rep.summary
    out = [f"# Version-skew check: `{rep.before}` → `{rep.after}`", "",
           f"| Pair | Verdict |", "|---|---|", f"| {PAIR_A} | **{s['pair_a']}** |", f"| {PAIR_B} | **{s['pair_b']}** |",
           "", f"**Recommendation:** {s['recommendation']}", "",
           "| Operation | Linter | Old code / new schema | New code / old schema |", "|---|---|---|---|"]
    for op in rep.ops:
        flag, rule = linter_verdict(op)
        va = next(v for v in rep.verdicts if v.op is op and v.pair == PAIR_A)
        vb = next(v for v in rep.verdicts if v.op is op and v.pair == PAIR_B)
        out.append(f"| `{op.describe()}` | {rule or 'ok'} | {va.status}: {va.reason} | {vb.status}: {vb.reason} |")
    for n in rep.notes:
        out.append(f"\n> {n}")
    return "\n".join(out) + "\n"
