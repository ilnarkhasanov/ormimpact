import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def django_shop(tmp_path) -> Path:
    dst = tmp_path / "django_shop"
    shutil.copytree(FIXTURES / "django_shop", dst)
    return dst


@pytest.fixture
def sa_shop(tmp_path) -> Path:
    dst = tmp_path / "sa_shop"
    shutil.copytree(FIXTURES / "sa_shop", dst)
    return dst


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin",
                               "HOME": str(cwd)}).stdout


def init_repo(path: Path) -> None:
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
