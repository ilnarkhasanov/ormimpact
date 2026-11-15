"""Evaluation against version history.

For a commit that changes the domain model, the parent revision is the
'before' tree and the parent plus only the commit's model files is the 'after'
tree (the protocol of Qiu et al., FSE 2013). The ground truth is the set of
other files the developers modified in the same commit. Each detector
configuration predicts a set of files; precision and recall are computed at
file level.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core import (
    LAYER_CHECKER, LAYER_CONFIG, LAYER_DATA, LAYER_DYNAMIC, LAYER_HISTORY, LAYER_ORM, LAYER_SQL,
    LAYER_TEMPLATE, LAYER_TEXT, at_least,
)
from .detectors.base import Options
from .diff import diff_registries
from .engine import CheckerSettings, analyze_trees
from .gitutil import GitError, changed_files, commit_date, commit_subject, export_tree, git, log_files, parents, show
from .project import Project, is_migration
from .registry import build_registry

L1 = {LAYER_CHECKER}
L2 = {LAYER_ORM, LAYER_CONFIG, LAYER_DYNAMIC}
L3 = {LAYER_SQL, LAYER_DATA, LAYER_TEMPLATE}
L4 = {LAYER_HISTORY}

CONFIGS: dict[str, set[str]] = {
    "checker (L1)": L1,
    "orm-aware (L2)": L2,
    "sql+data+templates (L3)": L3,
    "co-change (L4)": L4,
    "text baseline": {LAYER_TEXT},
    "L1+L2": L1 | L2,
    "L1+L2+L3": L1 | L2 | L3,
    "L1+L2+L3+L4": L1 | L2 | L3 | L4,
    "L2+L3 (no checker)": L2 | L3,
}

GT_SUFFIXES = {".py", ".html", ".htm", ".jinja", ".jinja2", ".j2", ".djhtml", ".sql", ".json", ".yaml",
               ".yml", ".csv", ".xml"}
GT_EXCLUDE = re.compile(r"(^|/)(docs?|\.github)/|(^|/)(CHANGELOG|CHANGES|HISTORY|NEWS)[^/]*$|\.lock$|"
                        r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|requirements[^/]*|package(-lock)?\.json|"
                        r"tsconfig\.json|conftest\.py)$", re.IGNORECASE)
MODEL_MARKERS = re.compile(
    r"models\.\w*(Field|ForeignKey)\b|\b(ForeignKey|OneToOneField|ManyToManyField)\(|Mapped\[|mapped_column\(|"
    r"\bColumn\(|relationship\(|__tablename__|db\.Column\("
)


@dataclass
class Metrics:
    tp: int
    fp: int
    fn: int
    predicted: int
    precision: float | None
    recall: float | None
    f1: float | None


def metrics(pred: set[str], gt: set[str]) -> Metrics:
    tp = len(pred & gt)
    fp = len(pred - gt)
    fn = len(gt - pred)
    p = tp / len(pred) if pred else None
    r = tp / len(gt) if gt else None
    f1 = (2 * p * r / (p + r)) if p is not None and r is not None and (p + r) > 0 else (0.0 if p is not None and r is not None else None)
    return Metrics(tp, fp, fn, len(pred), p, r, f1)


@dataclass
class CommitResult:
    sha: str
    subject: str = ""
    date: str = ""
    status: str = "ok"
    reason: str = ""
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model_files: list[str] = field(default_factory=list)
    ground_truth: list[str] = field(default_factory=list)
    predictions: dict[str, list[str]] = field(default_factory=dict)
    metrics: dict[str, dict] = field(default_factory=dict)
    checker: dict | None = None
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    category: str = ""
    kind_stats: dict[str, dict] = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)


def category_of(ops: set[str]) -> str:
    """breaking: remove/rename/extract; retype; add-only."""
    if ops & {"remove", "rename", "extract"}:
        return "breaking"
    if "retype" in ops:
        return "retype"
    return "add-only"


@dataclass
class EvalSettings:
    layers: list[str]
    checker: CheckerSettings
    min_confidence: str = "low"
    history_commits: int = 1000
    gt_suffixes: set[str] = field(default_factory=lambda: set(GT_SUFFIXES))
    keep_findings: bool = False
    subdir: str = ""
    categories: set[str] | None = None


def model_touching_commits(repo: Path, rev: str = "HEAD", max_scan: int = 2000) -> list[tuple[str, str, list[str]]]:
    """Commits whose diff adds or removes lines with model declarations in .py files."""
    out = []
    for sha, paths in log_files(repo, rev, max_scan):
        py = [p for p in paths if p.endswith(".py") and not is_migration(p)]
        if not py:
            continue
        try:
            diff = git(repo, "show", "--format=", "-U0", "--no-color", sha, "--", *py[:50])
        except GitError:
            continue
        hits = set()
        current = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and current:
                if MODEL_MARKERS.search(line):
                    hits.add(current)
        if hits:
            out.append((sha, commit_subject(repo, sha), sorted(hits)))
    return out


def evaluate_commit(repo: Path, sha: str, settings: EvalSettings) -> CommitResult:
    t0 = time.time()
    res = CommitResult(sha=sha)
    try:
        res.subject = commit_subject(repo, sha)
        res.date = commit_date(repo, sha)
        ps = parents(repo, sha)
    except GitError as e:
        res.status, res.reason = "error", str(e)
        return res
    if len(ps) != 1:
        res.status, res.reason = "skipped", "merge or root commit"
        return res
    parent = ps[0]
    files = changed_files(repo, parent, sha)
    prefix = settings.subdir.strip("/") + "/" if settings.subdir else ""
    with tempfile.TemporaryDirectory(prefix="ormimpact-eval-") as tmp:
        before = Path(tmp) / "before"
        after = Path(tmp) / "after"
        export_tree(repo, parent, before)
        root_b = before / settings.subdir if settings.subdir else before
        project = Project(root_b)
        reg_b = build_registry(project)
        model_files_b = reg_b.model_files()
        rel = lambda p: p[len(prefix):] if p.startswith(prefix) else None  # noqa: E731
        changed_py = [(st, rel(p), old) for st, p, old in files if p.endswith(".py") and rel(p)]
        overlay: list[str] = []
        for st, p, old in changed_py:
            if st in ("M", "R") and (p in model_files_b or (old and rel(old) in model_files_b)):
                overlay.append(p)
            elif st == "A":
                text = show(repo, sha, prefix + p) or ""
                if MODEL_MARKERS.search(text):
                    overlay.append(p)
        if not any(p in model_files_b for p in overlay):
            res.status, res.reason = "skipped", "no model file changed"
            res.seconds = round(time.time() - t0, 2)
            return res
        shutil.copytree(before, after, symlinks=True)
        root_a = after / settings.subdir if settings.subdir else after
        for p in overlay:
            text = show(repo, sha, prefix + p)
            if text is None:
                continue
            target = root_a / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # files renamed away from a model path: remove the old copy in 'after'
        for st, p, old in changed_py:
            if st == "R" and old and rel(old) and (root_a / rel(old)).exists() and p in overlay:
                (root_a / rel(old)).unlink()
        reg_a = build_registry(Project(root_a))
        changed_model_files = {p for p in overlay if p in model_files_b} | {
            rel(old) for st, p, old in changed_py if st == "R" and old and rel(old) in model_files_b}
        changes, notes = diff_registries(reg_b, reg_a, only_paths=changed_model_files)
        res.notes = notes
        res.model_files = sorted(changed_model_files)
        if not changes:
            res.status, res.reason = "skipped", "no field-level change detected"
            res.seconds = round(time.time() - t0, 2)
            return res
        res.changes = [c.describe() for c in changes]
        res.category = category_of({c.op for c in changes})
        if settings.categories and res.category not in settings.categories:
            res.status, res.reason = "skipped", f"category {res.category}"
            res.seconds = round(time.time() - t0, 2)
            return res
        # ground truth
        excluded = set(changed_model_files) | set(overlay)
        gt = set()
        for st, p, old in files:
            path = rel(old) if st == "R" and old else rel(p)
            if path is None or st == "A":
                continue  # new files cannot be predicted
            if path in excluded or is_migration(path) or GT_EXCLUDE.search(path):
                continue
            if Path(path).suffix.lower() not in settings.gt_suffixes:
                continue
            gt.add(path)
        res.ground_truth = sorted(gt)
        history = None
        if LAYER_HISTORY in settings.layers:
            try:
                raw = log_files(repo, parent, settings.history_commits)
                history = [(s, [p[len(prefix):] for p in ps_ if p.startswith(prefix)]) for s, ps_ in raw]
            except GitError as e:
                res.warnings.append(f"history: {e}")
                history = []
        options = Options(layers=settings.layers, history_rev=parent)
        report = analyze_trees(root_b, root_a, changes, reg_b, project, settings.layers, settings.checker,
                               edited=set(overlay), options=options, history_commits=history)
        res.checker = report.checker
        res.warnings += report.warnings
        valid = lambda p: p not in excluded and not is_migration(p) and (root_b / p).exists()  # noqa: E731
        for name, layers in CONFIGS.items():
            if not layers & set(settings.layers):
                continue
            pred = {f.path for f in report.findings if f.layer in layers
                    and at_least(f.confidence, settings.min_confidence) and valid(f.path)}
            res.predictions[name] = sorted(pred)
            res.metrics[name] = asdict(metrics(pred, gt))
        # per detector kind: which predicted files were in the ground truth
        kinds: dict[str, set[str]] = {}
        for f in report.findings:
            if at_least(f.confidence, settings.min_confidence) and valid(f.path):
                k = f"{f.layer}:{f.kind.split(':')[0] if f.layer == 'checker' else f.kind}"
                kinds.setdefault(k, set()).add(f.path)
        res.kind_stats = {k: {"tp": len(v & gt), "fp": len(v - gt)} for k, v in kinds.items()}
        if settings.keep_findings:
            res.findings = [f.to_dict() for f in report.findings]
    res.seconds = round(time.time() - t0, 2)
    return res


def aggregate(results: list[CommitResult], by_category: bool = True) -> dict:
    ok = [r for r in results if r.status == "ok"]
    out: dict = {"commits": len(results), "evaluated": len(ok),
                 "with_ground_truth": sum(1 for r in ok if r.ground_truth), "configs": {}}
    if by_category:
        out["categories"] = {}
        for cat in ("breaking", "retype", "add-only"):
            sub = [r for r in ok if r.category == cat]
            if sub:
                out["categories"][cat] = aggregate(sub, by_category=False)
        kinds: dict[str, dict[str, int]] = {}
        for r in ok:
            for k, v in r.kind_stats.items():
                d = kinds.setdefault(k, {"tp": 0, "fp": 0, "commits": 0})
                d["tp"] += v["tp"]
                d["fp"] += v["fp"]
                d["commits"] += 1
        for d in kinds.values():
            d["precision"] = d["tp"] / (d["tp"] + d["fp"]) if d["tp"] + d["fp"] else None
        out["kinds"] = dict(sorted(kinds.items(), key=lambda kv: -(kv[1]["tp"] + kv[1]["fp"])))
    names = []
    for r in ok:
        for n in r.metrics:
            if n not in names:
                names.append(n)
    for n in names:
        tp = sum(r.metrics[n]["tp"] for r in ok if n in r.metrics)
        fp = sum(r.metrics[n]["fp"] for r in ok if n in r.metrics)
        fn = sum(r.metrics[n]["fn"] for r in ok if n in r.metrics)
        precs = [r.metrics[n]["precision"] for r in ok if n in r.metrics and r.metrics[n]["precision"] is not None]
        recs = [r.metrics[n]["recall"] for r in ok if n in r.metrics and r.metrics[n]["recall"] is not None]
        micro_p = tp / (tp + fp) if tp + fp else None
        micro_r = tp / (tp + fn) if tp + fn else None
        out["configs"][n] = {
            "tp": tp, "fp": fp, "fn": fn,
            "micro_precision": micro_p, "micro_recall": micro_r,
            "micro_f1": (2 * micro_p * micro_r / (micro_p + micro_r)) if micro_p and micro_r else None,
            "macro_precision": sum(precs) / len(precs) if precs else None,
            "macro_recall": sum(recs) / len(recs) if recs else None,
            "commits_with_predictions": len(precs),
        }
    return out


def format_table(agg: dict, title: str = "all evaluated commits") -> str:
    def pct(x):
        return "   -  " if x is None else f"{x * 100:5.1f}%"

    lines = [f"== {title}: commits {agg['commits']}, evaluated {agg['evaluated']}, "
             f"with ground truth {agg['with_ground_truth']}",
             "",
             f"{'configuration':<26} {'TP':>5} {'FP':>6} {'FN':>5}  {'P(micro)':>8} {'R(micro)':>8} {'F1':>7}  "
             f"{'P(macro)':>8} {'R(macro)':>8}"]
    for n, m in agg["configs"].items():
        lines.append(f"{n:<26} {m['tp']:>5} {m['fp']:>6} {m['fn']:>5}  {pct(m['micro_precision']):>8} "
                     f"{pct(m['micro_recall']):>8} {pct(m['micro_f1']):>7}  {pct(m['macro_precision']):>8} "
                     f"{pct(m['macro_recall']):>8}")
    for cat, sub in agg.get("categories", {}).items():
        lines += ["", format_table(sub, f"{cat} changes")]
    if agg.get("kinds"):
        lines += ["", "== file-level precision per detector kind (top 25)", "",
                  f"{'kind':<48} {'TP':>5} {'FP':>5} {'P':>7} {'commits':>8}"]
        for k, d in list(agg["kinds"].items())[:25]:
            lines.append(f"{k:<48} {d['tp']:>5} {d['fp']:>5} {pct(d['precision']):>7} {d['commits']:>8}")
    return "\n".join(lines)
