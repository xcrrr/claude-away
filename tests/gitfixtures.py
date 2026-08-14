"""Helpers for building real Git repositories in temporary directories.

Real repositories, not mocks. A mocked Git only proves that our mock agrees with our
assumptions, and every interesting bug in this area lives in the gap between what we assume
Git does and what it actually does -- porcelain field counts, rename records, how status
reports a filename containing a newline.

Every repository is created under ``tmp_path`` with its own identity and an isolated
environment, so nothing here can read or write a developer's real repositories or their
global Git configuration, and nothing needs the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = ["GitRepo", "make_repo"]

#: Redirect variables cleared so the *fixture itself* is hermetic. Without this a test
#: that sets GIT_DIR to prove the production code ignores it would find its own helper
#: silently talking to the decoy repository -- which is exactly what happened once.
_REDIRECT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
)

_ISOLATED_ENV = {
    # Point Git's config discovery at nothing so the developer's ~/.gitconfig cannot change
    # test behaviour (hooks, templates, init.defaultBranch, autocrlf...).
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "LC_ALL": "C",
}


def _environment() -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if k not in _REDIRECT_ENV}
    environment.update(_ISOLATED_ENV)
    return environment


class GitRepo:
    """A throwaway repository under a temporary directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = _environment()
        return subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            capture_output=True,
            text=True,
            check=check,
            env=environment,
            timeout=60,
        )

    def write(self, relative: str, content: str = "x") -> Path:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit_all(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def make_repo(path: Path, *, initial_branch: str = "main", initial_commit: bool = True) -> GitRepo:
    """Create an isolated repository at ``path``.

    ``initial_branch`` is passed explicitly rather than relying on the Git default, which
    varies by version and by the developer's configuration -- and which the production code
    deliberately refuses to guess.
    """
    path.mkdir(parents=True, exist_ok=True)
    environment = _environment()
    subprocess.run(
        ["git", "init", "-q", f"--initial-branch={initial_branch}", str(path)],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=60,
    )
    repo = GitRepo(path)
    repo.git("config", "user.name", "Test")
    repo.git("config", "user.email", "test@example.invalid")
    repo.git("config", "commit.gpgsign", "false")
    if initial_commit:
        repo.write("README.md", "initial\n")
        repo.commit_all("initial commit")
    return repo
