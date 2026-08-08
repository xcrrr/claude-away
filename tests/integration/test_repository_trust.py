"""The repository is not trusted, and the tests that say so.

A repository's ``.git/config`` is not data, it is *configuration that names commands Git
runs*. ``core.fsmonitor`` fires on every ``git status``; a ``filter.<driver>.clean`` fires
whenever a tracked file's content has to be examined. Before this suite existed,
``inspect_repository`` -- the function whose docstring says it never mutates anything --
executed both, in the controller process, chosen by the repository being inspected.

Two consequences, and the second is the worse one. Code execution is bad; an ``fsmonitor``
hook that answers "nothing changed" is worse, because it makes ``git status`` report a
modified tracked file as clean, which turns ``resolve_expected_base`` into a function that
says "resolved" about somebody else's uncommitted work.

Every test here was written against a demonstrated exploit, and every one of them was
observed failing before the fix.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_away.adapters.git import (
    _PINNED_CONFIG,
    GitRunner,
    _repository_defined_command_config,
    inspect_repository,
)
from claude_away.errors import UnsafeRepositoryConfigError
from tests.gitfixtures import make_repo


def marker_script(tmp_path: Path, marker: Path, *, exit_code: int = 1) -> Path:
    """A hook that records having run. Not a payload -- just proof of execution."""
    script = tmp_path / "hook.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


class TestFsmonitorCannotExecute:
    def test_inspection_does_not_run_the_repositorys_fsmonitor(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        marker = tmp_path / "EXECUTED"
        repo.git("config", "core.fsmonitor", str(marker_script(tmp_path, marker)))

        inspect_repository(repo.path)

        assert not marker.exists(), "core.fsmonitor ran during a read-only inspection"

    def test_a_hostile_fsmonitor_cannot_forge_a_clean_worktree(self, tmp_path: Path) -> None:
        """The finding that matters most: the hook decides what Git believes changed.

        A v2 hook that always answers "nothing changed" made a genuinely modified tracked
        file invisible to `git status`, so `is_clean` was True and the base resolved --
        exactly the uncommitted work `base_revision` exists to refuse to build on.
        """
        repo = make_repo(tmp_path / "r")
        repo.write("tracked.txt", "original\n")
        repo.commit_all("add tracked file")

        liar = tmp_path / "liar.sh"
        liar.write_text('#!/bin/sh\nprintf "token\\0"\nexit 0\n', encoding="utf-8")
        liar.chmod(0o755)
        repo.git("config", "core.fsmonitor", str(liar))
        # Prime the index extension the way a real fsmonitor deployment would be.
        repo.git("status", check=False)
        repo.git("status", check=False)

        repo.write("tracked.txt", "SOMEBODY ELSE'S UNCOMMITTED WORK\n")
        os.utime(repo.path / "tracked.txt", (0, 0))  # defeat the stat-based shortcut too

        status = inspect_repository(repo.path).status
        assert not status.is_clean
        assert "tracked.txt" in status.unstaged

    def test_the_override_is_present_on_every_invocation(self, tmp_path: Path) -> None:
        """Not just on `status`: any command added later inherits the same pinning."""
        repo = make_repo(tmp_path / "r")
        argv = GitRunner(repo.path).run("rev-parse", "HEAD").args
        assert "-c" in argv
        assert "core.fsmonitor=false" in argv

    def test_every_pinned_key_is_actually_passed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        argv = list(GitRunner(repo.path).run("rev-parse", "HEAD").args)
        for key, value in _PINNED_CONFIG:
            assert f"{key}={value}" in argv, key


class TestEnvironmentInjection:
    @pytest.mark.parametrize(
        "variable,value",
        [
            ("GIT_CONFIG_PARAMETERS", "'core.fsmonitor={script}'"),
            ("GIT_CONFIG_COUNT", "1"),
        ],
    )
    def test_git_config_environment_cannot_inject_a_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
    ) -> None:
        """GIT_CONFIG_PARAMETERS was missing from the old enumerated deny list.

        One variable is enough to force any configuration key into every "read-only" call,
        including one that names a command. The list is now the whole GIT_* namespace.
        """
        repo = make_repo(tmp_path / "r")
        marker = tmp_path / "EXECUTED"
        script = marker_script(tmp_path, marker)

        monkeypatch.setenv(variable, value.format(script=script))
        if variable == "GIT_CONFIG_COUNT":
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(script))

        inspect_repository(repo.path)
        assert not marker.exists()

    def test_the_whole_git_namespace_is_removed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        environment = GitRunner(repo.path)._environment()
        leaked = [
            key
            for key in environment
            if key.startswith("GIT_") and key not in {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS"}
        ]
        assert leaked == []

    def test_a_future_git_variable_is_stripped_without_being_enumerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the prefix rule: no list to keep up to date."""
        repo = make_repo(tmp_path / "r")
        monkeypatch.setenv("GIT_SOME_VARIABLE_INVENTED_IN_2027", "x")
        assert "GIT_SOME_VARIABLE_INVENTED_IN_2027" not in GitRunner(repo.path)._environment()

    def test_ordinary_variables_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PATH in particular: stripping it would break the `git` lookup itself."""
        repo = make_repo(tmp_path / "r")
        monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "kept")
        environment = GitRunner(repo.path)._environment()
        assert environment["SOME_UNRELATED_VARIABLE"] == "kept"
        assert "PATH" in environment


class TestUnpinnableCommandKeysAreRefused:
    def test_a_repository_local_filter_driver_is_refused_before_it_runs(
        self, tmp_path: Path
    ) -> None:
        """`filter.<name>.clean` cannot be pinned: the driver name is user-chosen."""
        repo = make_repo(tmp_path / "r")
        marker = tmp_path / "EXECUTED"
        repo.write(".gitattributes", "* filter=evil\n")
        repo.write("tracked.txt", "content\n")
        repo.commit_all("add attributes")
        repo.git("config", "filter.evil.clean", f"touch {marker}; cat")
        repo.write("tracked.txt", "changed\n")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)

        assert caught.value.details["keys"] == ["filter.evil.clean"]
        assert not marker.exists()

    @pytest.mark.parametrize(
        "key",
        [
            "filter.x.clean",
            "filter.x.smudge",
            "filter.x.process",
            "diff.x.textconv",
            "diff.x.command",
            "merge.x.driver",
            "trailer.x.command",
            "gpg.ssh.program",
            "credential.https://example.invalid.helper",
        ],
    )
    def test_each_unpinnable_key_is_caught(self, tmp_path: Path, key: str) -> None:
        repo = make_repo(tmp_path / "r")
        repo.git("config", key, "/bin/true")
        with pytest.raises(UnsafeRepositoryConfigError):
            inspect_repository(repo.path)

    def test_the_error_names_every_offending_key(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.git("config", "filter.a.clean", "/bin/true")
        repo.git("config", "merge.b.driver", "/bin/true")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert set(caught.value.details["keys"]) == {"filter.a.clean", "merge.b.driver"}

    def test_a_globally_configured_filter_is_not_refused(self, tmp_path: Path) -> None:
        """`git lfs install` writes filter.lfs.* globally.

        Refusing every LFS repository would be a bug wearing a safeguard's clothes. The
        operator's own configuration is trusted; the repository's is not.
        """
        repo = make_repo(tmp_path / "r")
        global_config = tmp_path / "gitconfig"
        global_config.write_text(
            '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n', encoding="utf-8"
        )

        runner = GitRunner(repo.path)
        environment = dict(os.environ)
        environment["GIT_CONFIG_GLOBAL"] = str(global_config)
        completed = subprocess.run(
            ["git", "-C", str(repo.path), "config", "--list", "--show-scope", "-z"],
            capture_output=True,
            env=environment,
            timeout=60,
            check=True,
        )
        scopes = {field.decode() for field in completed.stdout.split(b"\x00")[::2] if field}
        assert "global" in scopes, "fixture did not actually install a global filter"
        assert _repository_defined_command_config(runner) == []

    def test_an_ordinary_repository_is_not_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.git("config", "core.autocrlf", "false")
        repo.git("config", "branch.main.remote", "origin")
        assert _repository_defined_command_config(GitRunner(repo.path)) == []

    def test_the_audit_parses_real_git_output(self, tmp_path: Path) -> None:
        """Pinned against bytes Git actually emits, not against a fixture I wrote.

        The first version of this parser assumed `<scope>\\n<key>\\n<value>` records; the
        real `-z` format is alternating `<scope>` and `<key>\\n<value>` fields. It parsed
        cleanly and found nothing -- the failure mode a security check must not have.
        """
        repo = make_repo(tmp_path / "r")
        repo.git("config", "filter.needle.clean", "/bin/true")

        raw = GitRunner(repo.path).run("config", "--list", "--show-scope", "-z").stdout
        fields = [field for field in raw.split(b"\x00") if field]
        assert b"local" in fields, "scope field is not where the parser expects it"
        assert any(field.startswith(b"filter.needle.clean\n") for field in fields)
        assert _repository_defined_command_config(GitRunner(repo.path)) == ["filter.needle.clean"]


class TestDiscoveredDefaultBranchProvenance:
    def test_a_configured_branch_is_marked_as_operator_declared(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        inspection = inspect_repository(repo.path, configured_default_branch="main")
        assert inspection.default_branch == "main"
        assert inspection.default_branch_source == "configured"

    def test_a_discovered_branch_is_marked_as_coming_from_the_repository(
        self, tmp_path: Path
    ) -> None:
        """The caller must be able to tell a declaration from the repository's assertion."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        repo.git("update-ref", "refs/remotes/origin/trunk", repo.head())
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        inspection = inspect_repository(repo.path)
        assert inspection.default_branch == "trunk"
        assert inspection.default_branch_source == "origin_head"

    def test_a_repository_written_init_default_branch_is_ignored(self, tmp_path: Path) -> None:
        """One appended line in .git/config used to move protection off the real default."""
        repo = make_repo(tmp_path / "r", initial_branch="main")
        with (repo.path / ".git" / "config").open("a", encoding="utf-8") as handle:
            handle.write("[init]\n\tdefaultBranch = attacker-branch\n")
        inspection = inspect_repository(repo.path)
        assert inspection.default_branch is None
        assert inspection.default_branch_source is None
