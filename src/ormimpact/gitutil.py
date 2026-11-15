"""Thin wrappers around the git CLI."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(root: str | Path, *args: str, check: bool = True, timeout: int = 600) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def toplevel(root: str | Path) -> Path | None:
    try:
        out = git(root, "rev-parse", "--show-toplevel", check=True).strip()
    except (GitError, FileNotFoundError, OSError):
        return None
    return Path(out).resolve() if out else None


def rev_parse(root: str | Path, rev: str) -> str:
    return git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def parents(root: str | Path, rev: str) -> list[str]:
    out = git(root, "rev-list", "--parents", "-n", "1", rev).split()
    return out[1:]


def export_tree(root: str | Path, rev: str, dest: str | Path) -> None:
    """Write the tree of ``rev`` to ``dest`` (like a checkout, without .git)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["git", "-C", str(root), "archive", "--format=tar", rev], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdout is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(dest)
    proc.wait()
    if proc.returncode != 0:
        raise GitError(f"git archive {rev} failed: {proc.stderr.read().decode(errors='replace') if proc.stderr else ''}")


def changed_files(root: str | Path, a: str, b: str) -> list[tuple[str, str, str | None]]:
    """[(status, path, old_path)] between two revisions; renames detected."""
    out = git(root, "diff", "--name-status", "-M", "--no-color", a, b)
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        st = parts[0][0]
        if st == "R" and len(parts) >= 3:
            res.append(("R", parts[2], parts[1]))
        elif len(parts) >= 2:
            res.append((st, parts[1], None))
    return res


def show(root: str | Path, rev: str, path: str) -> str | None:
    try:
        return git(root, "show", f"{rev}:{path}")
    except GitError:
        return None


def log_files(root: str | Path, rev: str, max_commits: int) -> list[tuple[str, list[str]]]:
    """[(sha, [paths])] for non-merge commits reachable from ``rev``, newest first."""
    out = git(root, "log", "--no-merges", "--name-only", "--no-renames", "--format=%x00%H", "-n",
              str(max_commits), rev)
    commits = []
    for chunk in out.split("\x00"):
        lines = [l for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        commits.append((lines[0].strip(), lines[1:]))
    return commits


def commit_subject(root: str | Path, rev: str) -> str:
    return git(root, "log", "-1", "--format=%s", rev).strip()


def commit_date(root: str | Path, rev: str) -> str:
    return git(root, "log", "-1", "--format=%cI", rev).strip()
