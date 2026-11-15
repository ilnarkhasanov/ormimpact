"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import __version__
from . import checker as chk
from .core import ALL_LAYERS, CONFIDENCE_ORDER, DEFAULT_LAYERS, LAYER_CHECKER, LAYER_HISTORY, LAYER_TEXT, parse_change
from .detectors.base import Options
from .diff import diff_registries
from .engine import CheckerSettings, analyze, analyze_trees
from .gitutil import GitError, export_tree, git, toplevel
from .project import Project
from .registry import build_registry
from .report import to_json, to_markdown, to_text

EPILOG = """\
change specifications:
  remove Order.total
  rename Order.total -> amount
  retype Order.total : Mapped[Decimal]          (SQLAlchemy; for the checker run)
  retype Order.total : DecimalField             (Django; field class or full expression)
  add Order.discount : Mapped[int]
  extract Order.delivery_address,delivery_city -> Address

layers: checker, orm, config, dynamic, sql, data, template, history, text (baseline, off by default)
"""


def _common(p: argparse.ArgumentParser, eval_mode: bool = False) -> None:
    p.add_argument("--layers", help=f"comma-separated layers (default: {','.join(DEFAULT_LAYERS)}"
                                    + (",text" if eval_mode else "") + ")")
    p.add_argument("--checker", default="none" if eval_mode else "auto",
                   choices=["auto", "mypy", "pyright", "none"],
                   help="type checker for the checker layer (default: %(default)s)")
    p.add_argument("--checker-args", default="", help="extra arguments for the checker, e.g. '--strict'")
    p.add_argument("--checker-targets", default="", help="paths to check, space-separated (default: .)")
    p.add_argument("--checker-cmd", default=None, help="full checker command line, overrides --checker-args")
    p.add_argument("--checker-timeout", type=int, default=3600)
    p.add_argument("--min-confidence", default="low", choices=list(CONFIDENCE_ORDER))
    p.add_argument("--history", type=int, default=1000, help="commits to mine for co-changes (default: 1000)")
    p.add_argument("--history-min-confidence", type=float, default=0.3)


def _output(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", "-f", default="text", choices=["text", "json", "md"])
    p.add_argument("--output", "-o", help="write the report to a file")
    p.add_argument("--verbose", "-v", action="store_true", help="show change, enclosing symbol and details")
    p.add_argument("--fail-on", choices=list(CONFIDENCE_ORDER), help="exit 1 if a finding of this confidence exists")
    p.add_argument("--include-migrations", action="store_true")
    p.add_argument("--exclude", action="append", default=[], help="directory name to skip (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ormimpact",
        description="ORM-aware impact analysis for domain-model changes (Django, SQLAlchemy).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
    )
    ap.add_argument("--version", action="version", version=f"ormimpact {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("models", help="list the models and fields found in a project")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("analyze", help="impact of a planned change", formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog=EPILOG)
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--change", "-c", action="append", required=True, help="change specification (repeatable)")
    _common(p)
    _output(p)

    p = sub.add_parser("diff", help="impact of the model changes in the working tree relative to a revision")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--base", default="HEAD", help="revision to compare with (default: HEAD)")
    _common(p)
    _output(p)

    p = sub.add_parser("mine", help="list commits that touch model declarations")
    p.add_argument("repo")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--max-commits", type=int, default=1000)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("eval", help="precision/recall against model-changing commits")
    p.add_argument("repo")
    p.add_argument("--commit", action="append", default=[], help="commit to evaluate (repeatable)")
    p.add_argument("--commits-file", help="file with one commit SHA per line")
    p.add_argument("--mine", type=int, default=0, help="evaluate up to N mined model-changing commits")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--max-scan", type=int, default=2000, help="commits to scan when mining")
    p.add_argument("--subdir", default="", help="project directory inside the repository")
    p.add_argument("--output", "-o", help="write JSON results to a file")
    p.add_argument("--keep-findings", action="store_true", help="store all findings in the JSON output")
    p.add_argument("--category", action="append", choices=["breaking", "retype", "add-only"],
                   help="only evaluate commits of this change category (repeatable)")
    _common(p, eval_mode=True)

    p = sub.add_parser("skew", help="version-skew check: is a release safe while old and new code share the schema?")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--base", default="HEAD", help="the running version (default: HEAD)")
    p.add_argument("--head", default=None, help="the new version (default: the working tree)")
    p.add_argument("--before-dir", help="compare two directories instead of git revisions: the old tree")
    p.add_argument("--after-dir", help="the new tree (with --before-dir)")
    p.add_argument("--subdir", default="")
    p.add_argument("--format", "-f", default="text", choices=["text", "json", "md"])
    p.add_argument("--output", "-o")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--fail-on", choices=["migrate-first", "deploy-first", "any", "no-safe-order"],
                   help="exit 1 when that deployment order breaks (for CI)")

    p = sub.add_parser("skew-eval", help="evaluate the version-skew check on commits that add migrations")
    p.add_argument("repo")
    p.add_argument("--commit", action="append", default=[])
    p.add_argument("--mine", type=int, default=0, help="evaluate up to N commits that add migrations")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--subdir", default="")
    p.add_argument("--oracle", choices=["none", "django-sqlite", "custom"], default="none")
    p.add_argument("--django-settings", help="settings module for the django-sqlite oracle, e.g. myproj.settings")
    p.add_argument("--oracle-python", default=sys.executable, help="interpreter with the project's dependencies")
    p.add_argument("--oracle-test-args", default="", help="extra arguments for the test command")
    p.add_argument("--oracle-parallel", type=int, default=4)
    p.add_argument("--oracle-base-python", action="append", default=[],
                   help="interpreter(s) to build per-version venvs with, tried in order (repeatable)")
    p.add_argument("--oracle-skip-package", action="append", default=[],
                   help="leave a requirement out of the per-version venvs, e.g. psycopg2 (repeatable)")
    p.add_argument("--oracle-requirements", action="append", default=[],
                   help="requirement file inside each tree; each version gets its own cached venv (repeatable)")
    p.add_argument("--prepare-cmd", help="custom oracle: create the schema in {db} (run in a tree)")
    p.add_argument("--test-cmd", help="custom oracle: run the tests against {db} (run in a tree)")
    p.add_argument("--pairs", default="A,B", help="oracle pairs to run: A, B or A,B")
    p.add_argument("--output", "-o")
    return ap


def _layers(arg: str | None, default: list[str]) -> list[str]:
    if not arg:
        return list(default)
    out = [x.strip() for x in arg.split(",") if x.strip()]
    bad = [x for x in out if x not in ALL_LAYERS]
    if bad:
        raise SystemExit(f"unknown layer(s): {', '.join(bad)}; choose from {', '.join(ALL_LAYERS)}")
    return out


def _checker(args) -> CheckerSettings:
    return CheckerSettings(
        tool=args.checker, args=args.checker_args.split() if args.checker_args else [],
        targets=args.checker_targets.split() if args.checker_targets else [],
        command=args.checker_cmd, timeout=args.checker_timeout,
    )


def _emit(report, args) -> int:
    if args.format == "json":
        text = to_json(report, args.min_confidence)
    elif args.format == "md":
        text = to_markdown(report, args.min_confidence)
    else:
        text = to_text(report, args.min_confidence, verbose=args.verbose,
                       color=sys.stdout.isatty() and not args.output)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"report written to {args.output} ({len(report.findings)} findings)")
    else:
        sys.stdout.write(text)
    if args.fail_on and any(CONFIDENCE_ORDER[f.confidence] >= CONFIDENCE_ORDER[args.fail_on] for f in report.findings):
        return 1
    return 0


def cmd_models(args) -> int:
    reg = build_registry(Project(args.root))
    if args.json:
        print(json.dumps([{**{k: v for k, v in asdict(m).items() if k not in ("fields", "reverse")},
                           "fields": {n: {"kind": f.kind, "column": f.column, "target": f.target,
                                          "inherited_from": f.inherited_from, "line": f.lineno}
                                      for n, f in m.fields.items()},
                           "reverse": {n: {"target": f.target, "origin": f.origin} for n, f in m.reverse.items()}}
                          for m in reg.models], indent=2))
        return 0
    if not reg.models:
        print("no models found")
        return 0
    for m in reg.models:
        flags = " (abstract)" if m.abstract else ""
        print(f"{m.name}{flags}  [{m.orm}]  {m.path}:{m.lineno}  table={m.table}")
        for n, f in m.fields.items():
            extra = []
            if f.column and f.column != n:
                extra.append(f"column={f.column}")
            if f.target:
                extra.append(f"-> {f.target}")
            if f.inherited_from:
                extra.append(f"from {f.inherited_from}")
            print(f"    {n:<28} {f.kind:<20} {' '.join(extra)}")
        for n, f in m.reverse.items():
            print(f"    {n:<28} {'reverse':<20} -> {f.target} (via {f.origin[0]}.{f.origin[1]})")
    return 0


def cmd_analyze(args) -> int:
    changes = []
    for spec in args.change:
        try:
            changes += parse_change(spec)
        except ValueError as e:
            raise SystemExit(str(e))
    layers = _layers(args.layers, DEFAULT_LAYERS)
    if args.checker == "none" and LAYER_CHECKER in layers:
        layers.remove(LAYER_CHECKER)
    options = Options(layers=layers, history_commits=args.history,
                      history_min_confidence=args.history_min_confidence)
    report = analyze(args.root, changes, layers=layers, checker=_checker(args), options=options,
                     include_migrations=args.include_migrations, exclude_dirs=tuple(args.exclude))
    return _emit(report, args)


def cmd_diff(args) -> int:
    root = Path(args.root).resolve()
    top = toplevel(root)
    if top is None:
        raise SystemExit("diff needs a git repository")
    sub = root.relative_to(top)
    layers = _layers(args.layers, DEFAULT_LAYERS)
    settings = _checker(args)
    if settings.tool == "auto":
        settings.tool = chk.auto_tool(root)
    if not settings.tool or settings.tool == "none":
        layers = [l for l in layers if l != LAYER_CHECKER]
    with tempfile.TemporaryDirectory(prefix="ormimpact-base-") as tmp:
        export_tree(top, args.base, Path(tmp))
        before_root = Path(tmp) / sub
        reg_before = build_registry(Project(before_root))
        now = Project(root, exclude_dirs=tuple(args.exclude), include_migrations=args.include_migrations)
        reg_after = build_registry(now)
        changes, notes = diff_registries(reg_before, reg_after)
        if not changes:
            print("no field-level model changes relative to", args.base)
            for n in notes:
                print("  note:", n)
            return 0
        edited = {p[len(str(sub)) + 1:] if str(sub) != "." else p
                  for p in git(top, "diff", "--name-only", args.base, "--", str(sub)).split()}
        options = Options(layers=layers, history_commits=args.history, history_rev=args.base,
                          history_min_confidence=args.history_min_confidence)
        report = analyze_trees(before_root, root, changes, reg_before, now, layers, settings, edited, options)
        report.notes = notes
        return _emit(report, args)


def cmd_mine(args) -> int:
    from .evaluate import model_touching_commits

    found = model_touching_commits(Path(args.repo), args.rev, args.max_commits)
    if args.json:
        print(json.dumps([{"sha": s, "subject": t, "files": f} for s, t, f in found], indent=2))
    else:
        for s, t, f in found:
            print(f"{s[:12]}  {t[:70]:<70}  {', '.join(f)[:80]}")
        print(f"{len(found)} commits touch model declarations")
    return 0


def cmd_eval(args) -> int:
    from .evaluate import EvalSettings, aggregate, evaluate_commit, format_table, model_touching_commits

    repo = Path(args.repo).resolve()
    layers = _layers(args.layers, DEFAULT_LAYERS + [LAYER_TEXT])
    settings = _checker(args)
    if settings.tool == "auto":
        settings.tool = chk.auto_tool(repo / args.subdir if args.subdir else repo)
    if not settings.tool or settings.tool == "none":
        layers = [l for l in layers if l != LAYER_CHECKER]
    shas = list(args.commit)
    if args.commits_file:
        shas += [l.split()[0] for l in Path(args.commits_file).read_text().splitlines() if l.strip()]
    es = EvalSettings(layers=layers, checker=settings, min_confidence=args.min_confidence,
                      history_commits=args.history, keep_findings=args.keep_findings, subdir=args.subdir,
                      categories=set(args.category) if args.category else None)
    results = []
    if args.mine:
        cands = model_touching_commits(repo, args.rev, args.max_scan)
        print(f"mined {len(cands)} candidate commits", file=sys.stderr)
        for sha, subject, _ in cands:
            if sum(1 for r in results if r.status == "ok") >= args.mine:
                break
            r = evaluate_commit(repo, sha, es)
            results.append(r)
            _progress(r)
    for sha in shas:
        r = evaluate_commit(repo, git(repo, "rev-parse", sha).strip(), es)
        results.append(r)
        _progress(r)
    agg = aggregate(results)
    print()
    print(format_table(agg))
    if args.output:
        Path(args.output).write_text(json.dumps({"aggregate": agg, "settings": {
            "layers": layers, "checker": settings.tool, "min_confidence": args.min_confidence,
            "checker_args": settings.args}, "commits": [asdict(r) for r in results]}, indent=2), encoding="utf-8")
        print(f"\nresults written to {args.output}")
    return 0


def _progress(r) -> None:
    if r.status == "ok":
        best = r.metrics.get("L1+L2+L3+L4") or r.metrics.get("L2+L3 (no checker)") or {}
        rec = best.get("recall")
        print(f"{r.sha[:10]}  ok       gt={len(r.ground_truth):<3} recall={'-' if rec is None else f'{rec:.2f}'}  "
              f"{'; '.join(r.changes)[:80]}", file=sys.stderr)
    else:
        print(f"{r.sha[:10]}  {r.status:<8} {r.reason}", file=sys.stderr)


def cmd_skew(args) -> int:
    from .skew import engine as sk

    if args.before_dir or args.after_dir:
        if not (args.before_dir and args.after_dir):
            raise SystemExit("--before-dir and --after-dir go together")
        rep = sk.analyze_trees(Path(args.before_dir), Path(args.after_dir))
    else:
        rep = sk.analyze_git(Path(args.root), args.base, args.head, subdir=args.subdir)
    text = {"text": lambda: sk.to_text(rep, args.verbose), "json": lambda: sk.to_json(rep),
            "md": lambda: sk.to_markdown(rep)}[args.format]()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"report written to {args.output}")
    else:
        sys.stdout.write(text)
    s = rep.summary
    if args.fail_on == "migrate-first" and s["pair_a"] == "breaks":
        return 1
    if args.fail_on == "deploy-first" and s["pair_b"] == "breaks":
        return 1
    if args.fail_on == "any" and "breaks" in (s["pair_a"], s["pair_b"]):
        return 1
    if args.fail_on == "no-safe-order" and s["pair_a"] == "breaks" and s["pair_b"] == "breaks":
        return 1
    return 0


def cmd_skew_eval(args) -> int:
    from .skew import evaluate as ev
    from .skew.oracle import OracleConfig, django_sqlite

    repo = Path(args.repo).resolve()
    oracle = None
    if args.oracle == "django-sqlite":
        if not args.django_settings:
            raise SystemExit("--oracle django-sqlite needs --django-settings")
        oracle = django_sqlite(args.django_settings, args.oracle_python, args.oracle_test_args,
                               args.oracle_parallel or None, args.oracle_requirements)
        if args.oracle_base_python:
            oracle.base_pythons = args.oracle_base_python
        oracle.skip_packages = args.oracle_skip_package
    elif args.oracle == "custom":
        if not (args.prepare_cmd and args.test_cmd):
            raise SystemExit("--oracle custom needs --prepare-cmd and --test-cmd")
        oracle = OracleConfig(prepare=args.prepare_cmd, test=args.test_cmd, python=args.oracle_python,
                              requirements=args.oracle_requirements)
        if args.oracle_base_python:
            oracle.base_pythons = args.oracle_base_python
        oracle.skip_packages = args.oracle_skip_package
    shas = [git(repo, "rev-parse", c).strip() for c in args.commit]
    if args.mine:
        shas += ev.migration_commits(repo, args.rev)[: args.mine]
    pairs = tuple(p.strip().upper() for p in args.pairs.split(",") if p.strip())
    rows = ev.run(repo, shas, oracle, pairs, args.subdir, args.output)
    print(ev.format_rows(rows))
    print()
    print(ev.format_aggregate(ev.aggregate(rows)))
    if args.output:
        print(f"\nresults written to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"models": cmd_models, "analyze": cmd_analyze, "diff": cmd_diff, "mine": cmd_mine,
                "eval": cmd_eval, "skew": cmd_skew, "skew-eval": cmd_skew_eval}[args.cmd](args)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
