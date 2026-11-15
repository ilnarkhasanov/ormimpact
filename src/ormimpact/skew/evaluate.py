"""Evaluate the version-skew check on the history of a repository.

For every commit that adds migration files: the static verdicts for both
pairs, the linter baseline, and, when an oracle is configured, the test-based
ground truth. Aggregates compare the static check and the linter with the
oracle.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..gitutil import GitError, commit_date, commit_subject, export_tree, git, parents
from .engine import analyze_git
from .oracle import OracleConfig, check
from .rules import PAIR_A, PAIR_B, linter_verdict


@dataclass
class CommitSkew:
    sha: str
    subject: str = ""
    date: str = ""
    status: str = "ok"
    reason: str = ""
    ops: list[str] = field(default_factory=list)
    static_a: str = ""
    static_b: str = ""
    linter: bool = False
    linter_rules: list[str] = field(default_factory=list)
    oracle_a: str = ""
    oracle_b: str = ""
    oracle_fail_a: int = 0
    oracle_fail_b: int = 0
    recommendation: str = ""
    verdicts: list[dict] = field(default_factory=list)
    seconds: float = 0.0


def migration_commits(repo: Path, rev: str = "HEAD", max_commits: int = 3000) -> list[str]:
    out = git(repo, "log", "--no-merges", "--diff-filter=A", "--name-only", "--format=%x00%H", "-n",
              str(max_commits), rev)
    shas = []
    for chunk in out.split("\x00"):
        lines = [l.strip() for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        files = [f for f in lines[1:] if f.endswith(".py") and not f.endswith("__init__.py")
                 and ("/migrations/" in f or "/versions/" in f)]
        if files:
            shas.append(lines[0])
    return shas


def evaluate_commit(repo: Path, sha: str, oracle: OracleConfig | None, pairs: tuple[str, ...] = ("A", "B"),
                    subdir: str = "") -> CommitSkew:
    t0 = time.time()
    r = CommitSkew(sha)
    try:
        r.subject, r.date = commit_subject(repo, sha), commit_date(repo, sha)
        ps = parents(repo, sha)
    except GitError as e:
        r.status, r.reason = "error", str(e)
        return r
    if len(ps) != 1:
        r.status, r.reason = "skipped", "merge or root commit"
        return r
    rep = analyze_git(repo, ps[0], sha, subdir=subdir)
    schema_ops = [o for o in rep.ops if o.kind not in ("data_migration",)]
    if not schema_ops:
        r.status, r.reason = "skipped", "no schema operations"
        r.seconds = round(time.time() - t0, 1)
        return r
    r.ops = [o.describe() for o in schema_ops]
    r.static_a, r.static_b = rep.summary["pair_a"], rep.summary["pair_b"]
    r.recommendation = rep.summary["recommendation"]
    flags = [linter_verdict(o) for o in schema_ops]
    r.linter = any(f for f, _ in flags)
    r.linter_rules = sorted({rule for f, rule in flags if f})
    r.verdicts = [v.to_dict() for v in rep.verdicts if v.status != "safe"]
    if oracle is not None:
        with tempfile.TemporaryDirectory(prefix="ormimpact-skeweval-") as tmp:
            b, a = Path(tmp) / "before", Path(tmp) / "after"
            export_tree(repo, ps[0], b)
            export_tree(repo, sha, a)
            res = check(b / subdir if subdir else b, a / subdir if subdir else a, oracle, pairs)
        if "A" in res:
            r.oracle_a, r.oracle_fail_a = res["A"].status, len(res["A"].new_failures)
        if "B" in res:
            r.oracle_b, r.oracle_fail_b = res["B"].status, len(res["B"].new_failures)
        if any(x.status == "error" for x in res.values()):
            r.reason = next(x.reason for x in res.values() if x.status == "error")[:500]
    r.seconds = round(time.time() - t0, 1)
    return r


def _confusion(rows, pred, truth) -> dict:
    tp = fp = fn = tn = 0
    for r in rows:
        p, t = pred(r), truth(r)
        if p and t:
            tp += 1
        elif p and not t:
            fp += 1
        elif t:
            fn += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec,
            "accuracy": (tp + tn) / len(rows) if rows else None}


def aggregate(rows: list[CommitSkew]) -> dict:
    ok = [r for r in rows if r.status == "ok"]
    out: dict = {"commits": len(rows), "with_schema_ops": len(ok)}
    la = [r for r in ok if r.oracle_a in ("breaks", "compatible")]
    lb = [r for r in ok if r.oracle_b in ("breaks", "compatible")]
    out["static"] = {
        "pair_a_breaks": sum(r.static_a == "breaks" for r in ok),
        "pair_b_breaks": sum(r.static_b == "breaks" for r in ok),
        "linter_flags": sum(r.linter for r in ok),
        "no_safe_order": sum(r.static_a == "breaks" and r.static_b == "breaks" for r in ok),
    }
    if la:
        out["pair_a"] = {
            "labelled": len(la),
            "oracle_breaks": sum(r.oracle_a == "breaks" for r in la),
            "static (breaks)": _confusion(la, lambda r: r.static_a == "breaks", lambda r: r.oracle_a == "breaks"),
            "static (breaks or risky)": _confusion(la, lambda r: r.static_a in ("breaks", "risky"),
                                                   lambda r: r.oracle_a == "breaks"),
            "linter": _confusion(la, lambda r: r.linter, lambda r: r.oracle_a == "breaks"),
        }
    if lb:
        out["pair_b"] = {
            "labelled": len(lb),
            "oracle_breaks": sum(r.oracle_b == "breaks" for r in lb),
            "static (breaks)": _confusion(lb, lambda r: r.static_b == "breaks", lambda r: r.oracle_b == "breaks"),
            "static (breaks or risky)": _confusion(lb, lambda r: r.static_b in ("breaks", "risky"),
                                                   lambda r: r.oracle_b == "breaks"),
            "linter (same flag)": _confusion(lb, lambda r: r.linter, lambda r: r.oracle_b == "breaks"),
        }
    return out


def format_rows(rows: list[CommitSkew]) -> str:
    lines = [f"{'commit':<10} {'static A':<8} {'oracle A':<10} {'static B':<8} {'oracle B':<10} {'linter':<6} operations"]
    for r in rows:
        if r.status != "ok":
            continue
        lines.append(f"{r.sha[:10]} {r.static_a:<8} {r.oracle_a or '-':<10} {r.static_b:<8} {r.oracle_b or '-':<10} "
                     f"{'flag' if r.linter else 'ok':<6} {'; '.join(r.ops)[:90]}")
    return "\n".join(lines)


def format_aggregate(agg: dict) -> str:
    def pct(x):
        return "  -  " if x is None else f"{x * 100:5.1f}%"

    lines = [f"commits with new migrations: {agg['commits']}, with schema operations: {agg['with_schema_ops']}",
             f"static: pair A breaks {agg['static']['pair_a_breaks']}, pair B breaks {agg['static']['pair_b_breaks']}, "
             f"no safe order {agg['static']['no_safe_order']}; linter flags {agg['static']['linter_flags']}"]
    for key, title in (("pair_a", PAIR_A), ("pair_b", PAIR_B)):
        if key not in agg:
            continue
        d = agg[key]
        lines += ["", f"== {title}: {d['labelled']} commits with an oracle verdict, {d['oracle_breaks']} break",
                  f"{'detector':<26} {'TP':>3} {'FP':>3} {'FN':>3} {'TN':>3}  {'precision':>9} {'recall':>7}"]
        for name, m in d.items():
            if isinstance(m, dict):
                lines.append(f"{name:<26} {m['tp']:>3} {m['fp']:>3} {m['fn']:>3} {m['tn']:>3}  "
                             f"{pct(m['precision']):>9} {pct(m['recall']):>7}")
    return "\n".join(lines)


def run(repo: Path, shas: list[str], oracle: OracleConfig | None, pairs=("A", "B"), subdir: str = "",
        output: str | None = None) -> list[CommitSkew]:
    rows = []
    for sha in shas:
        r = evaluate_commit(repo, sha, oracle, pairs, subdir)
        rows.append(r)
        if r.status == "ok":
            print(f"{sha[:10]}  static A={r.static_a:<7} B={r.static_b:<7} oracle A={r.oracle_a or '-':<10} "
                  f"B={r.oracle_b or '-':<10} linter={'flag' if r.linter else 'ok'}  {r.seconds}s  "
                  f"{'; '.join(r.ops)[:70]}", file=sys.stderr, flush=True)
        else:
            print(f"{sha[:10]}  {r.status}: {r.reason}", file=sys.stderr, flush=True)
        if output:
            Path(output).write_text(json.dumps({"aggregate": aggregate(rows), "commits": [asdict(x) for x in rows]},
                                               indent=2), encoding="utf-8")
    return rows
