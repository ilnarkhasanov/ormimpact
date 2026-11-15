import json
import shutil

import pytest

from conftest import git, init_repo
from ormimpact import checker as chk
from ormimpact.cli import main
from ormimpact.core import parse_change
from ormimpact.diff import diff_registries
from ormimpact.engine import CheckerSettings, analyze
from ormimpact.evaluate import CONFIGS, EvalSettings, evaluate_commit, model_touching_commits
from ormimpact.project import Project
from ormimpact.registry import build_registry


def _rename_total(repo):
    models = repo / "app/models.py"
    models.write_text(models.read_text().replace("    total: Mapped[int]", "    amount: Mapped[int]"))
    repo_py = repo / "app/repo.py"
    repo_py.write_text(repo_py.read_text().replace("Order.total", "Order.amount").replace("order.total", "order.amount")
                       .replace("filter_by(total=", "filter_by(amount="))
    (repo / "app/legacy.py").write_text("def legacy_total(order):\n    return order.amount * 2\n")


def test_diff_detects_rename_and_extract(sa_shop):
    before = build_registry(Project(sa_shop))
    m = sa_shop / "app/models.py"
    m.write_text(m.read_text().replace("    total: Mapped[int]", "    amount: Mapped[int]")
                 .replace("    note: Mapped[Optional[str]]\n", "    note_id: Mapped[int] = mapped_column(ForeignKey(\"notes.id\"))\n"
                          "    note_ref: Mapped[\"Note\"] = relationship()\n")
                 + "\n\nclass Note(Base):\n    __tablename__ = \"notes\"\n    id: Mapped[int] = mapped_column(primary_key=True)\n"
                   "    note: Mapped[Optional[str]]\n")
    after = build_registry(Project(sa_shop))
    changes, notes = diff_registries(before, after)
    ops = {(c.op, c.field) for c in changes}
    assert ("rename", "total") in ops
    assert ("extract", "note") in ops
    assert any(c.op == "add" and c.field == "note_ref" for c in changes)


def test_eval_on_synthetic_history(sa_shop):
    init_repo(sa_shop)
    _rename_total(sa_shop)
    git(sa_shop, "commit", "-qam", "rename total to amount")
    sha = git(sa_shop, "rev-parse", "HEAD").strip()
    assert [s for s, _, _ in model_touching_commits(sa_shop)][0] == sha
    settings = EvalSettings(layers=["orm", "config", "dynamic", "sql", "data", "template", "text"],
                            checker=CheckerSettings(tool="none"))
    res = evaluate_commit(sa_shop, sha, settings)
    assert res.status == "ok", res.reason
    assert res.changes == ["rename Order.total -> amount"]
    assert res.ground_truth == ["app/legacy.py", "app/repo.py"]
    m = res.metrics["orm-aware (L2)"]
    assert m["recall"] == 1.0
    assert res.metrics["text baseline"]["recall"] == 1.0


@pytest.mark.skipif(not chk.available("mypy"), reason="mypy not installed")
def test_eval_with_mypy_shows_blind_spot(sa_shop):
    init_repo(sa_shop)
    _rename_total(sa_shop)
    git(sa_shop, "commit", "-qam", "rename")
    sha = git(sa_shop, "rev-parse", "HEAD").strip()
    settings = EvalSettings(layers=["checker", "orm", "config", "dynamic", "sql", "data", "template"],
                            checker=CheckerSettings(tool="mypy"))
    res = evaluate_commit(sa_shop, sha, settings)
    assert res.status == "ok", res.reason
    # mypy sees repo.py but not the untyped legacy.py
    assert res.metrics["checker (L1)"]["recall"] == 0.5
    assert res.metrics["L1+L2"]["recall"] == 1.0
    assert res.checker["new_errors"] >= 2


@pytest.mark.skipif(not chk.available("mypy"), reason="mypy not installed")
def test_analyze_with_mypy(sa_shop):
    r = analyze(sa_shop, parse_change("remove Order.total"), checker=CheckerSettings(tool="mypy"))
    checker_files = {f.path for f in r.findings if f.layer == "checker"}
    assert checker_files == {"app/repo.py"}
    assert r.checker["new_errors"] >= 2


@pytest.mark.skipif(not chk.available("pyright"), reason="pyright not installed")
def test_analyze_with_pyright(sa_shop):
    (sa_shop / "pyrightconfig.json").write_text('{"typeCheckingMode": "basic"}')
    r = analyze(sa_shop, parse_change("remove Order.total"), checker=CheckerSettings(tool="pyright"))
    assert r.checker is not None
    assert "app/repo.py" in {f.path for f in r.findings if f.layer == "checker"}


def test_cli_analyze_json(django_shop, capsys):
    rc = main(["analyze", str(django_shop), "-c", "remove Order.status", "--checker", "none", "-f", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["findings"] > 0
    assert data["changes"][0]["description"] == "remove Order.status"


def test_cli_diff_and_history(django_shop, capsys):
    init_repo(django_shop)
    # history: views and serializers changed together with models twice
    for i in range(2):
        for p in ("shop/models.py", "shop/serializers.py"):
            f = django_shop / p
            f.write_text(f.read_text() + f"\n# touch {i}\n")
        git(django_shop, "commit", "-qam", f"touch {i}")
    m = django_shop / "shop/models.py"
    m.write_text(m.read_text().replace("    status = models.CharField", "    state = models.CharField"))
    rc = main(["diff", str(django_shop), "--checker", "none", "-f", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["changes"][0]["description"] == "rename Order.status -> state"
    layers = {f["layer"] for f in data["findings"]}
    assert {"orm", "config", "history"} <= layers


def test_cli_eval(sa_shop, capsys, tmp_path):
    init_repo(sa_shop)
    _rename_total(sa_shop)
    git(sa_shop, "commit", "-qam", "rename")
    out = tmp_path / "res.json"
    rc = main(["eval", str(sa_shop), "--mine", "5", "-o", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["aggregate"]["evaluated"] == 1
    assert set(data["aggregate"]["configs"]) <= set(CONFIGS)
