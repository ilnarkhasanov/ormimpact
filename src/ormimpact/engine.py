"""Analysis pipeline: registry -> detectors -> (checker) -> report."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import checker as chk
from .core import (
    ALL_LAYERS, CONFIDENCE_ORDER, DEFAULT_LAYERS, LAYER_CHECKER, LAYER_CONFIG, LAYER_DATA, LAYER_DYNAMIC,
    LAYER_HISTORY, LAYER_ORM, LAYER_SQL, LAYER_TEMPLATE, LAYER_TEXT, Change, Finding, at_least,
)
from .detectors import config, data, django_orm, dynamic, history, sql, sqlalchemy_orm, templates, text
from .detectors.base import Context, Options
from .edit import apply_to_tree, plan_edits
from .project import Project
from .registry import Registry, build_registry

DETECTORS = {
    LAYER_ORM: [django_orm.run, sqlalchemy_orm.run],
    LAYER_CONFIG: [config.run],
    LAYER_DYNAMIC: [dynamic.run],
    LAYER_SQL: [sql.run],
    LAYER_DATA: [data.run],
    LAYER_TEMPLATE: [templates.run],
    LAYER_HISTORY: [history.run],
    LAYER_TEXT: [text.run],
}


@dataclass
class CheckerSettings:
    tool: str | None = None           # mypy | pyright | None
    args: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    command: str | None = None
    timeout: int = 3600


@dataclass
class Report:
    root: str
    changes: list[Change]
    findings: list[Finding]
    warnings: list[str]
    layers: list[str]
    checker: dict | None = None
    models: int = 0
    orms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def files(self, min_confidence: str = "low", layers: set[str] | None = None) -> set[str]:
        return {f.path for f in self.findings if at_least(f.confidence, min_confidence)
                and (layers is None or f.layer in layers)}

    def to_dict(self) -> dict:
        by_layer: dict[str, int] = {}
        for f in self.findings:
            by_layer[f.layer] = by_layer.get(f.layer, 0) + 1
        return {
            "root": self.root,
            "changes": [c.to_dict() for c in self.changes],
            "summary": {
                "findings": len(self.findings),
                "files": len({f.path for f in self.findings}),
                "by_layer": by_layer,
                "by_confidence": {k: sum(1 for f in self.findings if f.confidence == k) for k in CONFIDENCE_ORDER},
            },
            "layers": self.layers,
            "checker": self.checker,
            "models": self.models,
            "orms": self.orms,
            "warnings": self.warnings,
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.findings],
        }


def dedupe(findings: list[Finding]) -> list[Finding]:
    best: dict[tuple, Finding] = {}
    for f in findings:
        k = f.key()
        if k not in best or f.rank > best[k].rank:
            best[k] = f
    out = list(best.values())
    out.sort(key=lambda f: (-f.rank, f.path, f.line, f.layer))
    return out


def run_detectors(project: Project, registry: Registry, changes: list[Change], layers: list[str],
                  options: Options | None = None) -> Context:
    options = options or Options(layers=layers)
    ctx = Context(project, registry, changes, options)
    for layer in layers:
        for fn in DETECTORS.get(layer, []):
            try:
                fn(ctx)
            except Exception as e:  # a detector must never take the whole analysis down
                ctx.warnings.append(f"{layer} detector failed: {e.__class__.__name__}: {e}")
    return ctx


def run_checker_layer(before_root: Path, after_root: Path, edited: set[str], settings: CheckerSettings,
                      changes: list[Change], project: Project) -> tuple[list[Finding], dict]:
    tool = settings.tool
    assert tool
    kw = dict(args=settings.args, targets=settings.targets, command=settings.command, timeout=settings.timeout)
    rb = chk.run_checker(tool, before_root, **kw)
    ra = chk.run_checker(tool, after_root, **kw)
    new = chk.new_errors(rb.diags, ra.diags, edited)

    def symbol(path: str, line: int):
        mod = project.module(path)
        return mod.symbol_at(line) if mod else None

    findings = chk.findings_from(new, tool, changes, lambda p, l: None)
    for f in findings:
        f.symbol = symbol(f.path, f.line)
    info = {
        "tool": tool,
        "command": " ".join(ra.command),
        "errors_before": len(rb.diags),
        "errors_after": len(ra.diags),
        "new_errors": len(new),
        "seconds": round(rb.seconds + ra.seconds, 1),
        "returncode": [rb.returncode, ra.returncode],
    }
    if not rb.diags and rb.returncode not in (0, 1) and rb.stderr:
        info["stderr"] = rb.stderr[-1500:]
    return findings, info


def analyze(
    root: str | Path,
    changes: list[Change],
    layers: list[str] | None = None,
    checker: CheckerSettings | None = None,
    options: Options | None = None,
    include_migrations: bool = False,
    exclude_dirs: tuple[str, ...] = (),
) -> Report:
    """Analyse a hypothetical change on the current tree of ``root``."""
    root = Path(root).resolve()
    layers = list(layers or DEFAULT_LAYERS)
    project = Project(root, exclude_dirs=exclude_dirs, include_migrations=include_migrations)
    registry = build_registry(project)
    warnings: list[str] = []
    norm = []
    for c in changes:
        c2, w = registry.normalize(c)
        warnings.extend(w)
        norm.append(c2)
    changes = norm
    options = options or Options(layers=layers)
    ctx = run_detectors(project, registry, changes, [l for l in layers if l != LAYER_CHECKER], options)
    findings = list(ctx.findings)
    warnings += ctx.warnings + project.errors[:20]
    checker_info = None
    if LAYER_CHECKER in layers:
        settings = checker or CheckerSettings()
        if settings.tool == "auto" or settings.tool is None:
            settings.tool = chk.auto_tool(root)
        if settings.tool and settings.tool != "none":
            if not chk.available(settings.tool):
                warnings.append(f"checker {settings.tool} is not installed; checker layer skipped")
            else:
                sources = {m.path: (root / m.path).read_text(encoding="utf-8") for m in registry.models}
                edits, w = plan_edits(registry, changes, sources)
                warnings += w
                with tempfile.TemporaryDirectory(prefix="ormimpact-after-") as tmp:
                    after = Path(tmp) / root.name
                    chk.copy_tree(root, after)
                    edited = apply_to_tree(after, edits)
                    cf, checker_info = run_checker_layer(root, after, edited, settings, changes, project)
                    findings += cf
        elif LAYER_CHECKER in layers and (checker is None or checker.tool != "none"):
            warnings.append("no mypy/pyright configuration found; checker layer skipped (use --checker mypy)")
    return Report(str(root), changes, dedupe(findings), warnings, layers, checker_info, len(registry),
                  sorted(registry.orms))


def analyze_trees(
    before_root: Path,
    after_root: Path | None,
    changes: list[Change],
    registry: Registry,
    project: Project,
    layers: list[str],
    checker: CheckerSettings | None,
    edited: set[str],
    options: Options | None = None,
    history_commits: list | None = None,
) -> Report:
    """Analyse given changes where the 'after' tree already exists (git diff / evaluation)."""
    options = options or Options(layers=layers)
    det_layers = [l for l in layers if l != LAYER_CHECKER]
    if history_commits is not None and LAYER_HISTORY in det_layers:
        det_layers = [l for l in det_layers if l != LAYER_HISTORY]
        ctx = run_detectors(project, registry, changes, det_layers, options)
        history.run_on_commits(ctx, history_commits)
    else:
        ctx = run_detectors(project, registry, changes, det_layers, options)
    findings = list(ctx.findings)
    warnings = list(ctx.warnings)
    info = None
    if LAYER_CHECKER in layers and checker and checker.tool and checker.tool != "none" and after_root is not None:
        if chk.available(checker.tool):
            cf, info = run_checker_layer(before_root, after_root, edited, checker, changes, project)
            findings += cf
        else:
            warnings.append(f"checker {checker.tool} is not installed; checker layer skipped")
    return Report(str(project.root), changes, dedupe(findings), warnings, layers, info, len(registry),
                  sorted(registry.orms))


__all__ = ["analyze", "analyze_trees", "Report", "CheckerSettings", "ALL_LAYERS", "DEFAULT_LAYERS"]
