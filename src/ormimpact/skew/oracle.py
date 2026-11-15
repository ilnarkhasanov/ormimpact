"""Dynamic oracle: run one version's test suite against the other version's schema.

For pair A (old code on the new schema) the schema is created by running the
new version's migrations; then the old version's tests run against that
database without migrating it back. Test failures that do not occur when the
old tests run on their own schema are the incompatibilities. Pair B is the
mirror image.

The commands are templates, so any framework can be plugged in; a recipe for
Django with SQLite is built in.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

FAIL_RE = re.compile(r"^(?:FAIL|ERROR): (.+?)\s*$", re.MULTILINE)
PYTEST_FAIL_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
RAN_RE = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
PYTEST_SUMMARY_RE = re.compile(r"=+ .*?(\d+) (?:passed|failed)", re.MULTILINE)

SETTINGS_MODULE = "_ormimpact_oracle_settings"


@dataclass
class OracleConfig:
    prepare: str                    # run in a tree: create the schema in the database {db}
    test: str                       # run in a tree: run the test suite against {db}
    env: dict[str, str] = field(default_factory=dict)
    test_env: dict[str, str] = field(default_factory=dict)      # extra variables for test runs only
    setup_files: dict[str, str] = field(default_factory=dict)   # files written into each tree first
    timeout: int = 3600
    python: str = sys.executable
    requirements: list[str] = field(default_factory=list)       # per-tree requirement files -> per-version venv
    extra_packages: list[str] = field(default_factory=lambda: ["tblib"])
    skip_packages: list[str] = field(default_factory=list)       # requirement names left out (e.g. psycopg2 on SQLite)
    venv_cache: str = str(Path.home() / ".cache" / "ormimpact" / "venvs")
    base_pythons: list[str] = field(default_factory=lambda: [sys.executable])

    def _filtered(self, req: Path, venv: Path, i: int) -> Path:
        if not self.skip_packages:
            return req
        skip = {p.lower().replace("_", "-") for p in self.skip_packages}
        keep = []
        for line in req.read_text().splitlines():
            name = re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")
            if name and name in skip:
                continue
            keep.append(line)
        out = venv.parent / f"{venv.name}-req{i}.txt"
        out.write_text("\n".join(keep) + "\n")
        return out

    def python_for(self, tree: Path) -> str:
        """The interpreter for a tree: a cached venv built from its requirement files, else ``python``."""
        files = [tree / r for r in self.requirements if (tree / r).exists()]
        if not files:
            return self.python
        h = hashlib.sha256()
        for f in files:
            h.update(f.read_bytes())
        h.update(" ".join(self.extra_packages).encode())
        h.update(" ".join(self.base_pythons).encode())
        h.update(" ".join(self.skip_packages).encode())
        venv = Path(self.venv_cache) / h.hexdigest()[:16]
        py = venv / "bin" / "python"
        if (venv / ".ok").exists():
            return str(py)
        if (venv / ".failed").exists():
            raise RuntimeError((venv / ".failed").read_text())
        venv.parent.mkdir(parents=True, exist_ok=True)
        errors = []
        for base in self.base_pythons:   # the first interpreter the requirements install on wins
            subprocess.run([base, "-m", "venv", "--clear", str(venv)], check=True, capture_output=True)
            args = [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
            for i, f in enumerate(files):
                args += ["-r", str(self._filtered(f, venv, i))]
            args += self.extra_packages
            p = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)
            if p.returncode == 0:
                (venv / ".ok").write_text(base)
                return str(py)
            errors.append(f"{base}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else p.returncode}")
        msg = "installing the requirements failed: " + " | ".join(errors)
        (venv / ".failed").write_text(msg)
        raise RuntimeError(msg)


@dataclass
class Run:
    ok: bool
    returncode: int
    tests: int
    failures: list[str]
    seconds: float
    tail: str = ""


@dataclass
class PairResult:
    pair: str
    status: str                     # breaks | compatible | error
    new_failures: list[str]
    baseline: Run | None = None
    skewed: Run | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("baseline", "skewed"):
            if d.get(k):
                d[k]["failures"] = d[k]["failures"][:50]
        return d


def django_sqlite(settings: str, python: str = sys.executable, test_args: str = "", parallel: int | None = 4,
                  requirements: list[str] | None = None) -> OracleConfig:
    """Recipe for Django projects that can run on SQLite."""
    override = (
        "import os\n"
        f"from {settings} import *  # noqa: F401,F403\n"
        f"from {settings} import DATABASES as _D\n"
        "_db = dict(_D['default'])\n"
        "_db.update({'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.environ['ORMIMPACT_DB']})\n"
        "_db['TEST'] = {'NAME': os.environ['ORMIMPACT_DB']}\n"
        "_db.pop('OPTIONS', None) if 'sqlite' not in str(_D['default'].get('ENGINE', '')) else None\n"
        "DATABASES = {'default': _db}\n"
        "if os.environ.get('ORMIMPACT_SKIP_MIGRATE'):\n"
        "    # the test runner must not migrate the prepared schema to its own version\n"
        "    from django.core.management.commands import migrate as _migrate\n"
        "    _migrate.Command.handle = lambda self, *args, **options: None\n"
    )
    par = f" --parallel {parallel}" if parallel else ""
    return OracleConfig(
        prepare=f"{{python}} manage.py migrate --noinput --settings={SETTINGS_MODULE}",
        test=f"{{python}} manage.py test --keepdb --noinput{par} --settings={SETTINGS_MODULE} {test_args}".strip(),
        setup_files={f"{SETTINGS_MODULE}.py": override},
        test_env={"ORMIMPACT_SKIP_MIGRATE": "1"},
        python=python,
        requirements=list(requirements or []),
    )


def _run(cmd: str, cwd: Path, db: Path, cfg: OracleConfig, test: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(cfg.env)
    if test:
        env.update(cfg.test_env)
    env["ORMIMPACT_DB"] = str(db)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return subprocess.run(cmd.format(db=db, tree=cwd, python=shlex.quote(cfg.python_for(cwd))), shell=True, cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=cfg.timeout, encoding="utf-8", errors="replace")


def _setup(tree: Path, cfg: OracleConfig) -> None:
    for rel, text in cfg.setup_files.items():
        (tree / rel).write_text(text, encoding="utf-8")


def prepare(tree: Path, db: Path, cfg: OracleConfig) -> tuple[bool, str]:
    _setup(tree, cfg)
    p = _run(cfg.prepare, tree, db, cfg)
    return p.returncode == 0, (p.stdout + p.stderr)[-3000:]


def run_tests(tree: Path, db: Path, cfg: OracleConfig) -> Run:
    _setup(tree, cfg)
    t0 = time.time()
    p = _run(cfg.test, tree, db, cfg, test=True)
    out = p.stdout + "\n" + p.stderr
    fails = sorted(f for f in set(FAIL_RE.findall(out)) | set(PYTEST_FAIL_RE.findall(out)) if not f.startswith("("))
    m = RAN_RE.search(out)
    tests = int(m.group(1)) if m else 0
    if not tests:
        m2 = PYTEST_SUMMARY_RE.search(out)
        tests = int(m2.group(1)) if m2 else 0
    return Run(p.returncode == 0, p.returncode, tests, fails, round(time.time() - t0, 1), out[-3000:])


def _copy_db(src: Path, dst: Path) -> None:
    dst.write_bytes(src.read_bytes())


def check(before: Path, after: Path, cfg: OracleConfig, pairs: tuple[str, ...] = ("A", "B")) -> dict[str, PairResult]:
    """Run the oracle for pair A (old tests on the new schema) and, if asked, pair B."""
    results: dict[str, PairResult] = {}
    with tempfile.TemporaryDirectory(prefix="ormimpact-oracle-") as tmp:
        tmp = Path(tmp)
        old_db, new_db = tmp / "schema_old.sqlite3", tmp / "schema_new.sqlite3"
        try:
            ok_old, log_old = prepare(before, old_db, cfg)
            ok_new, log_new = prepare(after, new_db, cfg)
        except (RuntimeError, subprocess.SubprocessError) as e:
            return {p: PairResult(p, "error", [], reason=f"environment: {e}") for p in pairs}
        if not ok_old or not ok_new:
            reason = "preparing the schema failed: " + (log_old if not ok_old else log_new)[-800:]
            return {p: PairResult(p, "error", [], reason=reason) for p in pairs}

        def pair(code: Path, own: Path, other: Path, name: str) -> PairResult:
            run_dir = tmp / name
            run_dir.mkdir()
            own_db, other_db = run_dir / "own.sqlite3", run_dir / "other.sqlite3"
            _copy_db(own, own_db)
            _copy_db(other, other_db)
            base = run_tests(code, own_db, cfg)
            skew = run_tests(code, other_db, cfg)
            if base.tests == 0 or skew.tests == 0:
                return PairResult(name, "error", [], base, skew,
                                  reason="the test command ran no tests: " + (skew.tail if skew.tests == 0 else base.tail)[-600:])
            new = sorted(set(skew.failures) - set(base.failures))
            return PairResult(name, "breaks" if new else "compatible", new, base, skew,
                              reason=f"{len(new)} tests fail only on the other version's schema" if new else
                              "no test fails only on the other version's schema")

        if "A" in pairs:
            results["A"] = pair(before, old_db, new_db, "A")
        if "B" in pairs:
            results["B"] = pair(after, new_db, old_db, "B")
    return results
