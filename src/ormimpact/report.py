"""Rendering reports as text, Markdown or JSON."""

from __future__ import annotations

import json
from collections import defaultdict

from .core import CONFIDENCE_ORDER, Finding, at_least
from .engine import Report

LAYER_TITLES = {
    "checker": "type checker",
    "orm": "ORM query API",
    "config": "class config",
    "dynamic": "dynamic/untyped",
    "sql": "raw SQL",
    "data": "fixtures/data",
    "template": "template",
    "history": "co-change",
    "text": "text match",
}


def _filtered(report: Report, min_confidence: str) -> list[Finding]:
    return [f for f in report.findings if at_least(f.confidence, min_confidence)]


def _by_file(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        groups[f.path].append(f)
    out = []
    for path, fs in groups.items():
        fs.sort(key=lambda f: (f.line, -f.rank))
        out.append((path, fs))
    out.sort(key=lambda kv: (-max(f.rank for f in kv[1]), kv[0]))
    return out


def to_json(report: Report, min_confidence: str = "low") -> str:
    d = report.to_dict()
    d["findings"] = [f.to_dict() for f in _filtered(report, min_confidence)]
    return json.dumps(d, indent=2, ensure_ascii=False)


def to_text(report: Report, min_confidence: str = "low", verbose: bool = False, color: bool = False) -> str:
    fs = _filtered(report, min_confidence)
    files = _by_file(fs)
    c = _Colors(color)
    lines = []
    orms = ", ".join(report.orms) or "no ORM"
    lines.append(c.bold(f"ormimpact: {len(fs)} findings in {len(files)} files") +
                 f"  ({orms}, {report.models} models, min confidence: {min_confidence})")
    for ch in report.changes:
        lines.append(f"  change: {ch.describe()}")
    if report.checker:
        ci = report.checker
        lines.append(f"  checker: {ci['tool']}: {ci['errors_before']} errors before, {ci['errors_after']} after, "
                     f"{ci['new_errors']} new ({ci['seconds']}s)")
    for w in report.warnings:
        lines.append(c.yellow(f"  warning: {w}"))
    for n in report.notes:
        lines.append(f"  note: {n}")
    counts = defaultdict(int)
    for f in fs:
        counts[f.layer] += 1
    if counts:
        lines.append("  by layer: " + ", ".join(f"{LAYER_TITLES.get(k, k)} {v}" for k, v in counts.items()))
    lines.append("")
    for path, group in files:
        lines.append(c.bold(path))
        for f in group:
            conf = {"high": c.red, "medium": c.yellow, "low": c.dim}[f.confidence](f"{f.confidence:<6}")
            loc = f"L{f.line}" if f.line else "  -"
            sym = f"  [{f.symbol}]" if f.symbol and verbose else ""
            lines.append(f"  {loc:>6}  {conf} {LAYER_TITLES.get(f.layer, f.layer):<15} {f.kind:<26} {f.evidence}{sym}")
            if verbose:
                extra = [f.change]
                if f.detail:
                    extra.append(f.detail)
                lines.append(c.dim(f"{'':>10}{' | '.join(extra)}"))
        lines.append("")
    if not files:
        lines.append("No affected locations found.")
    return "\n".join(lines).rstrip() + "\n"


def to_markdown(report: Report, min_confidence: str = "low") -> str:
    fs = _filtered(report, min_confidence)
    files = _by_file(fs)
    out = ["# Impact report", ""]
    out.append("**Changes:** " + "; ".join(f"`{c.describe()}`" for c in report.changes))
    out.append("")
    out.append(f"{len(fs)} findings in {len(files)} files; ORMs: {', '.join(report.orms) or '-'}; "
               f"{report.models} models.")
    if report.checker:
        ci = report.checker
        out.append(f"Type checker `{ci['tool']}`: {ci['new_errors']} new errors "
                   f"({ci['errors_before']} before, {ci['errors_after']} after).")
    for w in report.warnings:
        out.append(f"> warning: {w}")
    out.append("")
    for path, group in files:
        out.append(f"## `{path}`")
        out.append("")
        out.append("| Line | Confidence | Layer | Kind | Evidence |")
        out.append("|---:|---|---|---|---|")
        for f in group:
            ev = f.evidence.replace("|", "\\|")
            if f.detail:
                ev += f" <br><sub>{f.detail.replace('|', '/')}</sub>"
            out.append(f"| {f.line or ''} | {f.confidence} | {LAYER_TITLES.get(f.layer, f.layer)} | `{f.kind}` | `{ev}` |"
                       if "<br>" not in ev else
                       f"| {f.line or ''} | {f.confidence} | {LAYER_TITLES.get(f.layer, f.layer)} | `{f.kind}` | {ev} |")
        out.append("")
    return "\n".join(out)


class _Colors:
    def __init__(self, on: bool) -> None:
        self.on = on

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s):
        return self._w("1", s)

    def red(self, s):
        return self._w("31", s)

    def yellow(self, s):
        return self._w("33", s)

    def dim(self, s):
        return self._w("2", s)


__all__ = ["to_json", "to_text", "to_markdown", "CONFIDENCE_ORDER"]
