"""Shared pytest fixtures and git helpers.

The two fixture projects under ``tests/fixtures`` are copied into ``tmp_path`` for every test, so tests may edit,
commit and analyse them freely without touching the checked-in originals.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def django_shop(tmp_path) -> Path:
    """A fresh copy of the Django shop fixture (models, admin, forms, DRF serializers, views, factories,
    a JSON fixture, a raw SQL report and a template), each containing references the impact analysis must find."""
    dst = tmp_path / "django_shop"
    shutil.copytree(FIXTURES / "django_shop", dst)
    return dst


@pytest.fixture
def sa_shop(tmp_path) -> Path:
    """A fresh copy of the SQLAlchemy 2.0 shop fixture: ``Order.total`` is mapped to the column ``total_cents``,
    ``app/legacy.py`` is untyped (a blind spot for mypy) and ``sql/report.sql`` queries the table directly."""
    dst = tmp_path / "sa_shop"
    shutil.copytree(FIXTURES / "sa_shop", dst)
    return dst


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` with a fixed identity and an isolated ``HOME``, so the user's git config (hooks, signing,
    default branch) cannot influence the tests. Returns stdout; raises on a non-zero exit."""
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin",
                               "HOME": str(cwd)}).stdout


def init_repo(path: Path) -> None:
    """Turn ``path`` into a git repository with a single commit holding its current contents."""
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
