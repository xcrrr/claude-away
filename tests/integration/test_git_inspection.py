"""Read-only Git inspection against real repositories."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_away.adapters.git import (
    GitRunner,
    RepositoryOperation,
    inspect_repository,
    is_safe_ref,
    resolve_local_ref,
)
from claude_away.errors import (
    GitCommandError,
    GitOutputError,
    NotAGitRepositoryError,
    UnsupportedRepositoryError,
)
from tests.gitfixtures import make_repo


class TestBasicInspection:
    def test_clean_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        inspection = inspect_repository(repo.path)

        assert inspection.root == repo.path.resolve()
        assert inspection.branch == "main"
        assert not inspection.is_detached
        assert inspection.head_commit == repo.head()
        assert inspection.status.is_clean
        assert inspection.operations_in_progress == ()

    def test_staged_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("README.md", "changed\n")
        repo.git("add", "README.md")

        status = inspect_repository(repo.path).status
        assert status.staged == ("README.md",)
        assert not status.is_clean

    def test_unstaged_change(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("README.md", "changed\n")

        status = inspect_repository(repo.path).status
        assert status.unstaged == ("README.md",)
        assert status.staged == ()

    def test_untracked_file_counts_as_dirty(self, tmp_path: Path) -> None:
        """Untracked files are the ones a broad `git add` sweeps up by accident."""
        repo = make_repo(tmp_path / "r")
        repo.write("scratch.txt", "notes\n")

        status = inspect_repository(repo.path).status
        assert status.untracked == ("scratch.txt",)
        assert not status.is_clean

    def test_unborn_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r", initial_commit=False)
        inspection = inspect_repository(repo.path)

        assert inspection.is_unborn
        assert inspection.head_commit is None
        # An unborn HEAD is symbolic, so it is not detachment.
        assert not inspection.is_detached
        assert inspection.branch == "main"

    def test_detached_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("second.txt")
        repo.commit_all("second")
        repo.git("checkout", "-q", "--detach", "HEAD")

        inspection = inspect_repository(repo.path)
        assert inspection.is_detached
        assert inspection.branch is None
        assert inspection.head_commit is not None

    def test_repository_without_a_remote(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        assert inspect_repository(repo.path).remotes == ()


class TestHostileFilenames:
    """Status parsing must survive filenames Git allows and humans do not expect."""

    @pytest.mark.parametrize(
        "name",
        [
            "with space.txt",
            "with\ttab.txt",
            "-leading-dash.txt",
            "--looks-like-an-option.txt",
            "unicode-ф-é-😀.txt",
            "quote'single.txt",
            'quote"double.txt',
            "semi;colon.txt",
            "dollar$sign.txt",
            "back\\slash.txt",
            "newline\nin-name.txt",
        ],
    )
    def test_untracked_file_is_reported_verbatim(self, tmp_path: Path, name: str) -> None:
        repo = make_repo(tmp_path / "r")
        (repo.path / name).write_text("x", encoding="utf-8")

        status = inspect_repository(repo.path).status
        assert status.untracked == (name,), f"expected {name!r}, got {status.untracked!r}"

    def test_a_rename_does_not_desynchronise_the_parser(self, tmp_path: Path) -> None:
        """The classic porcelain-v2 -z bug.

        A rename record is followed by its original path in the *next* NUL-delimited field.
        A parser that treats every field as an entry misreads that original path as a new
        record and everything after it shifts by one.
        """
        repo = make_repo(tmp_path / "r")
        repo.write("original.txt", "content\n")
        repo.commit_all("add original")

        repo.git("mv", "original.txt", "renamed.txt")
        (repo.path / "after-the-rename.txt").write_text("u", encoding="utf-8")

        status = inspect_repository(repo.path).status
        assert status.staged == ("renamed.txt",)
        assert status.untracked == ("after-the-rename.txt",)

    def test_rename_to_a_newline_name_does_not_desynchronise(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("original.txt", "content\n")
        repo.commit_all("add original")

        repo.git("mv", "original.txt", "renamed\nwith newline.txt")
        (repo.path / "sentinel.txt").write_text("u", encoding="utf-8")

        status = inspect_repository(repo.path).status
        assert status.staged == ("renamed\nwith newline.txt",)
        # The sentinel proves the cursor stayed aligned past the rename record.
        assert status.untracked == ("sentinel.txt",)

    def test_paths_in_directories_with_spaces(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("dir with spaces/nested file.txt", "x")

        status = inspect_repository(repo.path).status
        assert status.untracked == ("dir with spaces/nested file.txt",)


class TestConflictAndOperations:
    def _conflicted(self, tmp_path: Path) -> Path:
        repo = make_repo(tmp_path / "r")
        repo.write("shared.txt", "base\n")
        repo.commit_all("base")

        repo.git("checkout", "-q", "-b", "feature")
        repo.write("shared.txt", "feature\n")
        repo.commit_all("feature change")

        repo.git("checkout", "-q", "main")
        repo.write("shared.txt", "main\n")
        repo.commit_all("main change")

        result = repo.git("merge", "feature", check=False)
        assert result.returncode != 0, "merge should have conflicted"
        return repo.path

    def test_unmerged_paths_are_reported(self, tmp_path: Path) -> None:
        path = self._conflicted(tmp_path)
        status = inspect_repository(path).status
        assert status.unmerged == ("shared.txt",)
        assert not status.is_clean

    def test_merge_in_progress_is_detected(self, tmp_path: Path) -> None:
        path = self._conflicted(tmp_path)
        inspection = inspect_repository(path)
        assert RepositoryOperation.MERGE in inspection.operations_in_progress

    def test_cherry_pick_in_progress_is_detected(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("shared.txt", "base\n")
        repo.commit_all("base")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("shared.txt", "side\n")
        side = repo.commit_all("side change")
        repo.git("checkout", "-q", "main")
        repo.write("shared.txt", "main\n")
        repo.commit_all("main change")

        result = repo.git("cherry-pick", side, check=False)
        assert result.returncode != 0

        inspection = inspect_repository(repo.path)
        assert RepositoryOperation.CHERRY_PICK in inspection.operations_in_progress


class TestSubmodules:
    def test_dirty_submodule_is_reported(self, tmp_path: Path) -> None:
        inner = make_repo(tmp_path / "inner")
        inner.write("lib.txt", "v1\n")
        inner.commit_all("lib")

        outer = make_repo(tmp_path / "outer")
        result = outer.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner.path),
            "vendor/inner",
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"submodule add unavailable in this environment: {result.stderr[:200]}")
        outer.commit_all("add submodule")

        (outer.path / "vendor" / "inner" / "lib.txt").write_text("dirty\n", encoding="utf-8")

        status = inspect_repository(outer.path).status
        assert status.dirty_submodules, "a modified submodule should be reported dirty"
        assert not status.is_clean


class TestUnsupportedRepositories:
    def test_bare_repository_is_refused(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True, timeout=60
        )
        with pytest.raises(UnsupportedRepositoryError, match="bare"):
            inspect_repository(bare)

    def test_plain_directory_is_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        with pytest.raises(NotAGitRepositoryError):
            inspect_repository(plain)


class TestEnvironmentIsolation:
    def test_git_dir_environment_cannot_redirect_inspection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GIT_DIR in the environment must not point inspection at another repository.

        Otherwise anything that can set an environment variable in the supervisor's process
        can make Claude Away describe repository A while acting on repository B.
        """
        target = make_repo(tmp_path / "target")
        decoy = make_repo(tmp_path / "decoy")
        decoy.write("decoy-only.txt", "x")
        decoy.commit_all("decoy commit")

        monkeypatch.setenv("GIT_DIR", str(decoy.path / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy.path))

        inspection = inspect_repository(target.path)
        assert inspection.root == target.path.resolve()
        assert inspection.head_commit == target.head()

    def test_git_config_environment_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hostile = tmp_path / "hostile.gitconfig"
        hostile.write_text("[core]\n\tpager = touch /tmp/pwned\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG", str(hostile))

        repo = make_repo(tmp_path / "r")
        assert inspect_repository(repo.path).status.is_clean


class TestRefSafety:
    @pytest.mark.parametrize(
        "ref",
        [
            "--upload-pack=touch /tmp/pwned",
            "-x",
            "--help",
            "with space",
            "bad..range",
            "trailing/",
            "/leading",
            "has^caret",
            "has:colon",
            "has?question",
            "has*star",
            "has[bracket",
            "has~tilde",
            "has@{reflog}",
            "back\\slash",
            "control\x01char",
            "",
        ],
    )
    def test_unsafe_refs_are_rejected(self, ref: str) -> None:
        assert not is_safe_ref(ref)

    @pytest.mark.parametrize(
        "ref", ["main", "master", "develop", "feature/thing", "release-1.2.3", "v1.0", "trunk"]
    )
    def test_ordinary_refs_are_accepted(self, ref: str) -> None:
        assert is_safe_ref(ref)

    def test_option_shaped_ref_is_refused_before_reaching_git(self, tmp_path: Path) -> None:
        """An option-shaped ref must never become argv, even though Git might accept it."""
        repo = make_repo(tmp_path / "r")
        with pytest.raises(GitOutputError, match="unsafe ref"):
            resolve_local_ref(repo.path, "--output=/tmp/pwned")

    def test_resolving_a_missing_ref_returns_none(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        assert resolve_local_ref(repo.path, "no-such-branch") is None

    def test_resolving_an_existing_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        assert resolve_local_ref(repo.path, "main") == repo.head()


class TestRunnerErrors:
    def test_failed_command_raises_typed_error(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        runner = GitRunner(repo.path)
        with pytest.raises(GitCommandError) as caught:
            runner.run("rev-parse", "--verify", "definitely-not-a-ref")
        assert caught.value.code == "git_command_failed"
        assert caught.value.returncode != 0

    def test_error_payload_does_not_include_the_environment(self, tmp_path: Path) -> None:
        """Structured detail for the supervisor, without leaking process environment."""
        repo = make_repo(tmp_path / "r")
        runner = GitRunner(repo.path)
        with pytest.raises(GitCommandError) as caught:
            runner.run("rev-parse", "--verify", "nope")
        payload = caught.value.to_dict()
        rendered = repr(payload)
        assert "environ" not in rendered
        assert set(payload["details"]) <= {"argv", "returncode", "stderr", "cwd"}

    def test_nul_byte_in_argument_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        with pytest.raises(GitOutputError, match="NUL"):
            GitRunner(repo.path).run("rev-parse", "bad\x00arg")

    def test_no_shell_is_used_anywhere(self) -> None:
        """A guard against the single mistake that would undo every ref-safety check.

        Parsed rather than grepped: the module docstring legitimately names ``shell=True``
        to explain why it is never used, and a substring search would match the explanation
        as readily as the mistake.
        """
        import ast

        source = Path(__file__).resolve().parents[2] / "src" / "claude_away" / "adapters" / "git.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        value = keyword.value
                        assert isinstance(value, ast.Constant) and value.value is False, (
                            "shell= must never be truthy in the Git adapter"
                        )
                function = node.func
                if isinstance(function, ast.Attribute):
                    assert function.attr not in {"system", "popen"}, (
                        f"os.{function.attr} must not be used in the Git adapter"
                    )


class TestDefaultBranchDiscovery:
    def test_configured_default_branch_wins(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        inspection = inspect_repository(repo.path, configured_default_branch="trunk")
        assert inspection.default_branch == "trunk"

    def test_unknown_default_branch_is_none_not_a_guess(self, tmp_path: Path) -> None:
        """No remote, no config: the honest answer is None, never "main"."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        environment = dict(os.environ)
        environment.pop("GIT_CONFIG_GLOBAL", None)
        inspection = inspect_repository(repo.path)
        assert inspection.default_branch in (None, "main", "master", "trunk")
        # The important part: whatever it says, it did not invent a branch that exists here.
        if inspection.default_branch is not None:
            assert inspection.default_branch != ""
