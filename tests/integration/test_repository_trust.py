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
from typing import Any

import pytest

from claude_away.adapters.git import (
    _PINNED_CONFIG,
    GitRunner,
    RepositoryOperation,
    _repository_defined_command_config,
    inspect_repository,
)
from claude_away.errors import UnsafeRepositoryConfigError, UnsupportedGitVersionError
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
        """Asserted against a written-out list, not against `_PINNED_CONFIG` itself.

        Iterating the constant under test made the check self-referential: deleting an entry
        simply shrank the loop, so twelve of the thirteen pinned keys could be removed with
        the suite green. The list below has to be edited deliberately, which is the point.
        """
        expected = {
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "core.pager=cat",
            "core.editor=false",
            "core.sshCommand=false",
            "core.askPass=",
            "core.gitProxy=",
            "core.alternateRefsCommand=",
            "credential.helper=",
            "diff.external=",
            "gpg.program=false",
            "uploadpack.packObjectsHook=",
            "protocol.ext.allow=never",
        }
        repo = make_repo(tmp_path / "r")
        argv = set(GitRunner(repo.path).run("rev-parse", "HEAD").args)

        assert expected <= argv, f"no longer pinned: {sorted(expected - argv)}"
        assert {f"{k}={v}" for k, v in _PINNED_CONFIG} == expected, (
            "the pinned set changed; update the expectation deliberately, with a reason"
        )


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

        The fixture cannot use `GIT_CONFIG_GLOBAL`, which is what an earlier version of this
        test did: the adapter strips the whole `GIT_*` namespace, so the hostile global
        config never reached the code under test and the assertion held vacuously. `HOME` is
        not stripped, so pointing it at a throwaway directory is the way to give the child a
        global config it will actually read.
        """
        repo = make_repo(tmp_path / "r")
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n', encoding="utf-8"
        )

        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("HOME", str(home))
            runner = GitRunner(repo.path)

            # The guard is only meaningful if the global filter genuinely reaches the child.
            raw = runner.run("config", "--list", "--show-scope", "-z").stdout
            fields = [f for f in raw.split(b"\x00") if f]
            scoped = list(zip(fields[::2], fields[1::2], strict=False))
            assert any(
                scope == b"global" and key.startswith(b"filter.lfs.clean") for scope, key in scoped
            ), "fixture did not actually install a global filter the adapter can see"

            assert _repository_defined_command_config(runner) == []

    def test_a_worktree_scoped_command_key_is_refused(self, tmp_path: Path) -> None:
        """`worktree` is half of `_UNTRUSTED_CONFIG_SCOPES` and nothing exercised it.

        A repository can set `extensions.worktreeConfig` and write the key into
        `.git/worktrees/<name>/config.worktree`, where plain `git config` never puts it.
        """
        repo = make_repo(tmp_path / "r")
        repo.git("config", "extensions.worktreeConfig", "true")
        repo.git("config", "--worktree", "filter.sneaky.clean", "/bin/true")

        raw = GitRunner(repo.path).run("config", "--list", "--show-scope", "-z").stdout
        fields = [f for f in raw.split(b"\x00") if f]
        scopes = set(fields[::2])
        assert b"worktree" in scopes, "fixture did not produce worktree scope"

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert "filter.sneaky.clean" in caught.value.details["keys"]

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


class TestIndexBitsCannotHideChanges:
    """`git status` does not report what the index tells it not to look at."""

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_a_hidden_modification_is_not_reported_clean(self, tmp_path: Path, flag: str) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("local.conf", "original\n")
        repo.commit_all("add config")
        repo.write("local.conf", "MODIFIED\n")
        repo.git("update-index", flag, "local.conf")

        # Ground truth: git status really does say nothing about it.
        assert repo.git("status", "--porcelain=v2", "-z").stdout == ""

        status = inspect_repository(repo.path).status
        assert status.unverifiable == ("local.conf",)
        assert not status.is_clean

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_the_base_refuses_with_an_actionable_reason(self, tmp_path: Path, flag: str) -> None:
        from claude_away.core.base_revision import BaseRefusal, resolve_expected_base

        repo = make_repo(tmp_path / "r")
        repo.write("local.conf", "original\n")
        repo.commit_all("add config")
        repo.git("update-index", flag, "local.conf")

        inspection = inspect_repository(repo.path, configured_default_branch="main")
        resolution = resolve_expected_base(inspection, project_id="api")

        assert not resolution.resolved
        assert BaseRefusal.UNVERIFIABLE_PATHS in resolution.refusals
        assert "update-index" in resolution.detail["unverifiable_hint"]

    def test_the_change_really_would_be_carried_into_a_new_branch(self, tmp_path: Path) -> None:
        """Why this is a refusal and not a warning."""
        repo = make_repo(tmp_path / "r")
        repo.write("local.conf", "original\n")
        repo.commit_all("add config")
        repo.write("local.conf", "MODIFIED\n")
        repo.git("update-index", "--assume-unchanged", "local.conf")

        repo.git("checkout", "-q", "-b", "claude-away-demo")
        assert (repo.path / "local.conf").read_text(encoding="utf-8") == "MODIFIED\n"

    def test_an_ordinary_repository_has_nothing_unverifiable(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        status = inspect_repository(repo.path).status
        assert status.unverifiable == ()
        assert status.is_clean


class TestPathsSurviveInspection:
    def test_a_root_ending_in_a_space_is_not_truncated(self, tmp_path: Path) -> None:
        """`.strip()` removed the trailing space along with git's newline.

        The result was a root that does not exist, and an enrolment error telling the
        operator to enrol a repository root they cannot enrol.
        """
        repo = make_repo(tmp_path / "trailing space ")
        inspection = inspect_repository(repo.path)
        assert str(inspection.root).endswith("trailing space ")
        assert inspection.root.exists()

    def test_a_non_utf8_root_keeps_operation_detection_working(self, tmp_path: Path) -> None:
        """`errors="replace"` produced a git_dir that did not exist.

        Interrupted-operation detection is pure filesystem probing against that path, so it
        returned "nothing in progress" for a repository sitting in a conflicted merge.
        """
        awkward = tmp_path / os.fsdecode(b"repo\xffdir")
        repo = make_repo(awkward)
        repo.write("f.txt", "base\n")
        repo.commit_all("base")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("f.txt", "side\n")
        repo.commit_all("side")
        repo.git("checkout", "-q", "main")
        repo.write("f.txt", "main\n")
        repo.commit_all("main")
        repo.git("merge", "side", check=False)

        inspection = inspect_repository(awkward)
        assert inspection.git_dir.exists()
        assert RepositoryOperation.MERGE in inspection.operations_in_progress


class TestTheAuditFailsClosed:
    """An audit that did not run must never read as "found nothing"."""

    def test_a_git_without_show_scope_refuses_rather_than_skipping_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This was a fail-open in the fix for the fail-open.

        `git config --list --show-scope` needs Git 2.26. On anything older the call exited
        129, the audit returned "no offending keys", and every hostile repository was
        accepted -- with the rest of the hardening in place, which made it look protected.
        """
        repo = make_repo(tmp_path / "r")
        repo.git("config", "filter.evil.clean", "/bin/true")

        shim_dir = tmp_path / "oldgit"
        shim_dir.mkdir()
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        shim = shim_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do\n'
            '  case "$a" in --show-scope)\n'
            '    echo "error: unknown option \\`show-scope\'" >&2; exit 129;;\n'
            "  esac\n"
            "done\n"
            f'exec {real_git} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")

        with pytest.raises(UnsupportedGitVersionError, match=r"2\.26"):
            inspect_repository(repo.path)

    def test_a_repository_with_no_configuration_at_all_is_not_refused(self, tmp_path: Path) -> None:
        """Exit 1 with no output means "nothing to list", which is not a failure."""
        repo = make_repo(tmp_path / "r")
        assert _repository_defined_command_config(GitRunner(repo.path)) == []


class TestGitVersionDiagnostic:
    def test_an_option_this_build_needs_is_reported_as_a_version_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not as "path is not a Git repository", which sent operators to check the config."""
        repo = make_repo(tmp_path / "r")

        shim_dir = tmp_path / "oldgit"
        shim_dir.mkdir()
        shim = shim_dir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do\n'
            '  case "$a" in --no-optional-locks)\n'
            '    echo "error: unknown option \\`no-optional-locks\'" >&2; exit 129;;\n'
            "  esac\n"
            "done\n"
            f"exec {subprocess.run(['which', 'git'], capture_output=True, text=True).stdout.strip()}"
            ' "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")

        with pytest.raises(UnsupportedGitVersionError, match=r"2\.26"):
            inspect_repository(repo.path)


class TestSubmoduleConfigIsAudited:
    """The audit must reach every config `git status` will read, not just the top one.

    `git status --ignore-submodules=none` spawns a child `git status` inside every gitlinked
    submodule, and that child reads the SUBMODULE's own .git/config. Auditing only the
    superproject let a repository put a filter driver one directory down and have the
    controller run it -- while the `-c` pins, which do propagate into the child, made the
    other half of the defence look like it was working.
    """

    def _superproject_with_hostile_submodule(self, tmp_path: Path) -> tuple[Path, Path]:
        marker = tmp_path / "EXECUTED"
        payload = tmp_path / "payload.sh"
        payload.write_text(
            f"#!/bin/sh\necho ran >> {marker}\nprintf 'hello\\n'\n", encoding="utf-8"
        )
        payload.chmod(0o755)

        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("a.txt", "hello\n")
        inner.write(".gitattributes", "a.txt filter=pwn\n")
        inner.commit_all("inner")
        inner.git("config", "--local", "filter.pwn.clean", str(payload))

        super_repo.write("top.txt", "top\n")
        super_repo.git("add", "top.txt", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "super", check=False)
        # A genuine, uncommitted divergence the hostile filter is designed to mask.
        inner.write("a.txt", "world\n")
        return super_repo.path, marker

    def test_a_submodules_filter_driver_is_refused(self, tmp_path: Path) -> None:
        root, marker = self._superproject_with_hostile_submodule(tmp_path)

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(root)

        assert caught.value.details["keys"] == ["sub:filter.pwn.clean"], "must name where"
        assert not marker.exists(), "the submodule's command ran during inspection"

    def test_the_superproject_config_alone_never_showed_it(self, tmp_path: Path) -> None:
        """Why the top-level audit could not have caught this: the key is not there."""
        root, _ = self._superproject_with_hostile_submodule(tmp_path)
        assert _repository_defined_command_config(GitRunner(root)) == []

    def test_a_gitlink_that_is_not_checked_out_is_skipped(self, tmp_path: Path) -> None:
        """A gitlink with no working tree has no config to read and no status to spawn."""
        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("a.txt", "x")
        inner.commit_all("inner")
        super_repo.git("add", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "add gitlink", check=False)

        import shutil

        shutil.rmtree(super_repo.path / "sub")
        inspect_repository(super_repo.path)  # must not raise

    def test_an_ordinary_submodule_still_inspects(self, tmp_path: Path) -> None:
        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("a.txt", "x")
        inner.commit_all("inner")
        super_repo.git("add", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "add gitlink", check=False)

        assert inspect_repository(super_repo.path).status.is_clean

    def test_a_submodule_whose_config_cannot_be_read_is_refused(self, tmp_path: Path) -> None:
        """ "We could not look" must never read as "nothing to find".

        Skipping was wrong twice over: `git status` descends regardless and fails there with
        an untyped error, and the whole point of this audit is that a check which did not
        run is not a pass.
        """
        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("f.txt", "x")
        inner.commit_all("inner")
        super_repo.git("add", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "add gitlink", check=False)

        (inner.path / ".git" / "config").write_text("[core\nnot valid config\n", encoding="utf-8")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(super_repo.path)
        assert caught.value.details["submodule"] == "sub"

    def test_a_gitlinked_directory_that_is_not_a_repository_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Git walks *up* from `-C`, so without the root check the child runner resolves
        back to the superproject and audits it again, once per level, learning nothing."""
        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("f.txt", "x")
        inner.commit_all("inner")
        super_repo.git("add", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "add gitlink", check=False)

        import shutil

        shutil.rmtree(inner.path / ".git")
        inspect_repository(super_repo.path)  # must not raise, must not recurse

    def test_nesting_is_bounded(self, tmp_path: Path) -> None:
        """Depth-limited so a pathological tree cannot turn inspection into a long walk."""
        from claude_away.adapters.git import _MAX_SUBMODULE_DEPTH

        assert _MAX_SUBMODULE_DEPTH >= 2


class TestSubmoduleReportingCannotBeSuppressed:
    """`--ignore-submodules=none` is the only thing defeating `ignore = all`, and it was untested."""

    def _dirty_submodule(self, tmp_path: Path, *, where: str) -> Path:
        super_repo = make_repo(tmp_path / "super")
        inner = make_repo(super_repo.path / "sub")
        inner.write("f.txt", "x")
        inner.commit_all("inner")
        super_repo.write(".gitmodules", '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n')
        super_repo.git("add", ".gitmodules", "sub", check=False)
        super_repo.git("commit", "-q", "-m", "add gitlink", check=False)
        inner.write("f.txt", "DIRTY")

        if where == "gitmodules":
            # A TRACKED file: this arrives through an ordinary commit or pull request, so it
            # needs no access to .git at all.
            super_repo.write(
                ".gitmodules",
                '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n',
            )
        else:
            # The config form applies only to a *registered* submodule, which is what
            # `git submodule init` produces.
            super_repo.git("config", "submodule.sub.url", "./sub")
            super_repo.git("config", "submodule.sub.ignore", "all")
        return super_repo.path

    @pytest.mark.parametrize("where", ["gitmodules", "config"])
    def test_a_dirty_submodule_is_reported_despite_ignore_all(
        self, tmp_path: Path, where: str
    ) -> None:
        root = self._dirty_submodule(tmp_path, where=where)

        # Ground truth: without the flag, git really does stay silent.
        quiet = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v2"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert "sub" not in quiet, "fixture did not actually suppress reporting"

        status = inspect_repository(root).status
        assert not status.is_clean
        assert [m.path for m in status.dirty_submodules] == ["sub"]


class TestEveryGitCallIsBounded:
    def test_the_timeout_reaches_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`GIT_TIMEOUT_SECONDS` appeared in no test: dropping `timeout=` was silent.

        An unattended run that hangs forever on a stuck lock is the failure the bound exists
        to prevent, and "forever" is not something a test suite notices by waiting.
        """
        import subprocess as subprocess_module

        seen: dict[str, Any] = {}
        real_run = subprocess_module.run

        def recording_run(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real_run(*args, **kwargs)

        repo = make_repo(tmp_path / "r")
        monkeypatch.setattr("claude_away.adapters.git.subprocess.run", recording_run)
        GitRunner(repo.path, timeout=17).run("rev-parse", "HEAD")

        assert seen["timeout"] == 17

    def test_a_timeout_becomes_a_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as subprocess_module

        from claude_away.errors import GitCommandError

        def always_timeout(*args: Any, **kwargs: Any) -> Any:
            raise subprocess_module.TimeoutExpired(cmd="git", timeout=1)

        repo = make_repo(tmp_path / "r")
        monkeypatch.setattr("claude_away.adapters.git.subprocess.run", always_timeout)
        with pytest.raises(GitCommandError, match="timed out"):
            GitRunner(repo.path).run("rev-parse", "HEAD")
