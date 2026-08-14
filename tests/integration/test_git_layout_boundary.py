"""The hermetic Git boundary: layout discovered from the filesystem, config default-deny.

Four review rounds each found a critical in the previous round's fix, and every one was the
same shape: a repository-controlled configuration key changed what the controller executed
or believed. The last two were the same key, ``core.worktree``, exploited two different ways.
Enumerating dangerous keys cannot be completed by inspection -- Git has hundreds and adds
more -- so this module tests the replacement:

1. the repository's layout is discovered from the *filesystem*, never from
   ``git rev-parse``, because that is precisely where ``core.worktree`` gets a vote;
2. repository-local configuration is validated against a small allow-list, default-deny, so
   an unknown key is refused rather than waved through;
3. every Git invocation is bound with explicit ``--git-dir`` and ``--work-tree``, so no
   configuration value can redirect what is inspected;
4. submodules are traversed manually by one shared recursive walker, so nested dirtiness
   cannot hide behind a parent that looks clean.

These tests were written before the implementation and observed failing against
``ea54c24``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from claude_away.adapters.git import GitRunner, inspect_repository
from claude_away.adapters.gitlayout import audit_local_config, discover_layout
from claude_away.errors import UnsafeRepositoryConfigError, UnsupportedRepositoryError
from tests.gitfixtures import make_repo


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def submodule(parent: Path, child_source: Path, name: str) -> Path:
    git(
        "-C",
        str(parent),
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child_source),
        name,
    )
    git("-C", str(parent), "commit", "-q", "-m", f"add {name}")
    return parent / name


class TestWorktreeRedirection:
    """``core.worktree`` must never choose what gets inspected."""

    def test_top_level_redirection_cannot_report_a_decoy_clean(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.write("a.txt", "pristine\n")
        repo.commit_all("init")

        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "a.txt").write_text("pristine\n", encoding="utf-8")

        repo.git("config", "core.worktree", str(decoy))
        (repo.path / "a.txt").write_text("SOMEONE ELSE'S WORK\n", encoding="utf-8")

        # Asserted as a refusal, not as "refusal OR dirty". Instrumentation showed the
        # `except: return` version never reached its assertion, so the test named a property
        # it did not check and duplicated a cheaper one. The bound runner's independent
        # behaviour -- that it inspects the enrolled tree even when the audit is bypassed --
        # is pinned by `test_a_bound_runner_ignores_core_worktree`.
        with pytest.raises(UnsafeRepositoryConfigError, match=r"core\.worktree"):
            inspect_repository(repo.path)
        assert (repo.path / "a.txt").read_text(encoding="utf-8").startswith("SOMEONE")

    def test_submodule_redirection_cannot_hide_uncommitted_work(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "pristine\n")
        source.commit_all("inner")

        super_repo = make_repo(tmp_path / "super")
        super_repo.write("t.txt", "t")
        super_repo.commit_all("init")
        child = submodule(super_repo.path, source.path, "mod")

        # The decoy must live INSIDE a repository, or the audit already fails closed on it
        # for an unrelated reason and the test proves nothing. A gitignored directory in the
        # superproject satisfies that, which is what makes the attack self-contained.
        decoy = super_repo.path / ".cache"
        decoy.mkdir()
        (decoy / "a.txt").write_text("pristine\n", encoding="utf-8")
        super_repo.write(".gitignore", "/.cache/\n")
        super_repo.git("add", "-A")
        super_repo.git("commit", "-q", "-m", "ignore cache")
        git(
            f"--git-dir={super_repo.path / '.git' / 'modules' / 'mod'}",
            "config",
            "core.worktree",
            str(decoy),
        )
        (child / "a.txt").write_text("SOMEONE ELSE'S WORK\n", encoding="utf-8")

        with pytest.raises(UnsafeRepositoryConfigError, match=r"core\.worktree"):
            inspect_repository(super_repo.path)


class TestNestedSubmodules:
    """Nested dirtiness must not disappear because its parent looks clean."""

    def _chain(self, tmp_path: Path) -> tuple[Path, Path]:
        deep_src = make_repo(tmp_path / "deepsrc")
        deep_src.write("d.txt", "pristine\n")
        deep_src.commit_all("deep")

        mid_src = make_repo(tmp_path / "midsrc")
        mid_src.write("m.txt", "m")
        mid_src.commit_all("mid")
        submodule(mid_src.path, deep_src.path, "deep")

        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("top")
        mid = submodule(top.path, mid_src.path, "mid")
        git(
            "-C",
            str(mid),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        )
        return top.path, mid / "deep"

    def test_modified_tracked_file_two_levels_down(self, tmp_path: Path) -> None:
        top, deep = self._chain(tmp_path)
        (deep / "d.txt").write_text("CHANGED\n", encoding="utf-8")
        assert not inspect_repository(top).status.is_clean

    def test_untracked_file_two_levels_down(self, tmp_path: Path) -> None:
        top, deep = self._chain(tmp_path)
        (deep / "left-behind.txt").write_text("x", encoding="utf-8")
        assert not inspect_repository(top).status.is_clean

    def test_assume_unchanged_two_levels_down(self, tmp_path: Path) -> None:
        top, deep = self._chain(tmp_path)
        (deep / "d.txt").write_text("CHANGED\n", encoding="utf-8")
        git("-C", str(deep), "update-index", "--assume-unchanged", "d.txt")
        assert not inspect_repository(top).status.is_clean

    def test_ignore_all_two_levels_down(self, tmp_path: Path) -> None:
        """``ignore = all`` is set in ``mid``'s own config, not in its ``.gitmodules``.

        Rewriting the tracked ``.gitmodules`` would make ``mid`` dirty by itself, so the
        assertion would hold even if the walker never reached ``deep`` -- a test that proves
        nothing. The config route changes no tracked content, so the only thing that can
        make this tree report dirty is the nested traversal.
        """
        top, deep = self._chain(tmp_path)
        (deep / "d.txt").write_text("CHANGED\n", encoding="utf-8")
        git("-C", str(deep.parent), "config", "submodule.deep.ignore", "all")

        quiet = subprocess.run(
            ["git", "-C", str(top), "status", "--porcelain=v2", "--ignore-submodules=none"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        assert "mid" not in quiet, "fixture did not actually suppress reporting"

        assert not inspect_repository(top).status.is_clean

    def test_a_refused_config_two_levels_down_refuses_the_whole_tree(self, tmp_path: Path) -> None:
        top, deep = self._chain(tmp_path)
        git("-C", str(deep), "config", "filter.pwn.clean", "/bin/true")
        with pytest.raises(UnsafeRepositoryConfigError):
            inspect_repository(top)


class TestDefaultDenyLocalConfig:
    def test_an_unknown_local_key_is_refused(self, tmp_path: Path) -> None:
        """Proves the polarity really is default-deny rather than a longer deny-list."""
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "futureClaudeAwayTest.someKey", "value")
        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert any("futureclaudeawaytest" in key.lower() for key in caught.value.details["keys"])

    def test_the_refusal_does_not_print_the_value(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "futureClaudeAwayTest.token", "SUPERSECRETVALUE")
        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert "SUPERSECRETVALUE" not in str(caught.value.to_dict())

    @pytest.mark.parametrize("directive", ["include.path", "includeIf.gitdir:/.path"])
    def test_local_includes_are_refused(self, tmp_path: Path, directive: str) -> None:
        """An include is a way to add config the audit never sees."""
        # An INERT included key. With a command-bearing one the effective-configuration
        # audit fired first and this test passed without the include branch ever running --
        # it could not tell the two refusals apart, because it asserted only the type.
        extra = tmp_path / "extra.cfg"
        extra.write_text("[futureclaudeawaytest]\n\tincluded = 1\n", encoding="utf-8")

        repo = make_repo(tmp_path / "repo")
        repo.git("config", directive, str(extra))
        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert any(key.lower().startswith("include") for key in caught.value.details["keys"])

    def test_an_included_secret_never_reaches_diagnostics(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra.cfg"
        extra.write_text("[secretsection]\n\ttoken = INCLUDEDSECRET\n", encoding="utf-8")
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "include.path", str(extra))
        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert "INCLUDEDSECRET" not in str(caught.value.to_dict())

    def test_remote_uploadpack_is_refused(self, tmp_path: Path) -> None:
        """Rejected as unsupported repository-controlled behaviour.

        Honest scope note: this did NOT execute in the reproduction against M2A's command
        set, because nothing here contacts a remote. It is refused because the allow-list
        is default-deny, not because a current exploit was demonstrated.
        """
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "remote.origin.uploadpack", "/bin/true")
        with pytest.raises(UnsafeRepositoryConfigError):
            inspect_repository(repo.path)


class TestOrdinaryRepositoriesStillWork:
    """Safety may refuse the unusual. It must not refuse every normal clone."""

    def test_git_init(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "plain")
        assert inspect_repository(repo.path).status.is_clean

    def test_a_normal_clone(self, tmp_path: Path) -> None:
        origin = make_repo(tmp_path / "origin")
        origin.write("a.txt", "x")
        origin.commit_all("first")
        clone = tmp_path / "clone"
        git("clone", "-q", str(origin.path), str(clone))
        assert inspect_repository(clone).status.is_clean

    def test_separate_git_dir(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        store = tmp_path / "store"
        git("init", "-q", f"--separate-git-dir={store}", str(worktree))
        git("-C", str(worktree), "config", "user.email", "a@b")
        git("-C", str(worktree), "config", "user.name", "a")
        (worktree / "a.txt").write_text("x", encoding="utf-8")
        git("-C", str(worktree), "add", "-A")
        git("-C", str(worktree), "commit", "-q", "-m", "i")
        assert inspect_repository(worktree).status.is_clean

    def test_a_linked_worktree(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "main")
        repo.write("a.txt", "x")
        repo.commit_all("i")
        linked = tmp_path / "linked"
        repo.git("worktree", "add", "-q", str(linked), "-b", "side")
        assert inspect_repository(linked).status.is_clean

    def test_extensions_worktree_config(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "extensions.worktreeConfig", "true")
        assert inspect_repository(repo.path).status.is_clean

    def test_an_initialised_submodule(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")
        super_repo = make_repo(tmp_path / "super")
        super_repo.write("t.txt", "t")
        super_repo.commit_all("init")
        submodule(super_repo.path, source.path, "mod")
        assert inspect_repository(super_repo.path).status.is_clean

    @pytest.mark.parametrize(
        "key,value",
        [
            ("user.name", "A Developer"),
            ("user.email", "dev@example.invalid"),
            ("core.filemode", "false"),
            ("core.ignorecase", "true"),
            ("core.precomposeunicode", "true"),
            ("core.autocrlf", "input"),
            ("core.symlinks", "true"),
            ("core.logallrefupdates", "true"),
        ],
    )
    def test_ordinary_local_settings_are_accepted(
        self, tmp_path: Path, key: str, value: str
    ) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", key, value)
        assert inspect_repository(repo.path).status.is_clean

    def test_remote_and_branch_tracking_metadata(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "remote.origin.url", "https://example.invalid/x.git")
        repo.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        repo.git("config", "branch.main.remote", "origin")
        repo.git("config", "branch.main.merge", "refs/heads/main")
        assert inspect_repository(repo.path).status.is_clean


class TestEveryInvocationIsBound:
    """Control 3 on its own, with the allow-list deliberately out of the way.

    Every one of these was written because a mutation of the guard survived the suite. The
    allow-list refuses a hostile ``core.worktree`` before the runner ever sees it, so a test
    that goes through ``inspect_repository`` cannot tell a bound runner from an unbound one.
    These drive the runner directly, or inspect the argv, so each control is pinned by
    itself rather than by the one in front of it.
    """

    def test_a_bound_runner_ignores_core_worktree(self, tmp_path: Path) -> None:
        """Kills: dropping ``--work-tree`` from the location arguments.

        ``core.worktree`` is set and *not* audited here. With the binding in place Git
        reports the enrolled tree's modification; without it, Git honours the value and
        describes the decoy, which is round four exactly.
        """
        repo = make_repo(tmp_path / "repo")
        repo.write("a.txt", "pristine\n")
        repo.commit_all("init")

        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "a.txt").write_text("pristine\n", encoding="utf-8")

        layout = discover_layout(repo.path)
        repo.git("config", "core.worktree", str(decoy))
        (repo.path / "a.txt").write_text("SOMEONE ELSE'S WORK\n", encoding="utf-8")

        output = (
            GitRunner(repo.path, layout=layout)
            .run(
                "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=all"
            )
            .stdout
        )
        assert b"a.txt" in output, "the decoy tree was described instead of the enrolled one"

    def _record(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        import subprocess as subprocess_module

        seen: list[list[str]] = []
        real_run = subprocess_module.run

        def recording_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(list(argv))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr("claude_away.adapters.git.subprocess.run", recording_run)
        return seen

    def _superproject(self, tmp_path: Path) -> Path:
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")
        super_repo = make_repo(tmp_path / "super")
        super_repo.write("t.txt", "t")
        super_repo.commit_all("init")
        submodule(super_repo.path, source.path, "mod")
        return super_repo.path

    def test_every_invocation_names_a_git_dir_and_a_work_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kills: dropping either location argument.

        ``--work-tree`` has a behavioural discriminator above. ``--git-dir`` does not have
        one for the current command set -- ``-C`` at a validated root finds the same git
        directory -- so it is pinned as a contract instead. That is the honest position:
        it is defence in depth, and a defence nothing checks is a defence that quietly
        disappears in the next refactor.
        """
        root = self._superproject(tmp_path)
        seen = self._record(monkeypatch)
        inspect_repository(root)

        # `git config --file <path> --no-includes` is the inert parse: it binds to one file,
        # starts no repository discovery, and deliberately predates the layout being trusted.
        # Everything else goes through `GitRunner`, which is what `--no-optional-locks` marks.
        runner_calls = [argv for argv in seen if "--no-optional-locks" in argv]
        assert runner_calls, "no GitRunner invocations were recorded"
        for argv in seen:
            if argv not in runner_calls:
                assert argv[:3] == ["git", "config", "--file"], argv
                continue
            assert any(a.startswith("--git-dir=") for a in argv), argv
            assert any(a.startswith("--work-tree=") for a in argv), argv
        seen = runner_calls

        # And both repositories in the tree were reached explicitly, not through a descent.
        bound = {a for argv in seen for a in argv if a.startswith("--work-tree=")}
        assert bound == {f"--work-tree={root}", f"--work-tree={root / 'mod'}"}

    def test_git_is_never_asked_to_descend_into_a_submodule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kills: switching the status command back to ``--ignore-submodules=none``.

        ``=none`` makes Git scan each submodule's *worktree*, using Git's own idea of where
        that worktree is. Nothing must ask for that: the content half comes from the walker,
        which validates each repository first. ``=dirty`` is the setting that keeps the
        gitlink *records* -- a staged bump, a deleted or replaced submodule directory --
        which ``=all`` suppressed and which the walker cannot reconstruct from the child.
        """
        root = self._superproject(tmp_path)
        seen = self._record(monkeypatch)
        inspect_repository(root)

        statuses = [argv for argv in seen if "status" in argv]
        assert statuses, "no status invocation was recorded"
        for argv in statuses:
            assert "--ignore-submodules=dirty" in argv, argv
            assert "--ignore-submodules=none" not in argv, argv


class TestTheAuditReadsWhatItClaimsTo:
    """More survivors of the mutation run, each pinned by the property it actually protects."""

    def test_a_worktree_scoped_unknown_key_is_refused(self, tmp_path: Path) -> None:
        """Kills: skipping ``config.worktree``.

        The existing worktree-scope test uses a *command-bearing* key, which the
        defence-in-depth scope audit catches on its own -- so the allow-list could stop
        reading ``config.worktree`` entirely with the suite green. An inert unknown key is
        visible only to the allow-list.
        """
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "extensions.worktreeConfig", "true")
        repo.git("config", "--worktree", "futureClaudeAwayTest.someKey", "value")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert any("futureclaudeawaytest" in key.lower() for key in caught.value.details["keys"])

    def test_a_malformed_included_file_still_produces_a_named_refusal(self, tmp_path: Path) -> None:
        """The allow-list must name an include without reading, or depending on, its target.

        This does *not* kill the "drop ``--no-includes``" mutation, and the honest reason is
        that the mutation is equivalent: ``git config --file`` does not expand includes in
        the first place -- verified directly, with a malformed included file, on this Git.
        ``--no-includes`` stays as explicit intent and as insurance against that default
        changing, not because it is load-bearing today.

        Driven against ``audit_local_config`` rather than ``inspect_repository``, because
        the effective-configuration check that runs first *does* expand includes and dies on
        the garbage before the allow-list is reached.
        """
        extra = tmp_path / "extra.cfg"
        extra.write_text("[core\nthis is not valid config\n", encoding="utf-8")

        repo = make_repo(tmp_path / "repo")
        repo.git("config", "include.path", str(extra))

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            audit_local_config(discover_layout(repo.path))
        assert caught.value.details["keys"] == ["include.path"]

    def test_core_worktree_pointing_elsewhere_is_refused_not_merely_ignored(
        self, tmp_path: Path
    ) -> None:
        """Kills: allowing ``core.worktree`` without comparing it to the enrolled tree.

        The binding already means the value cannot take effect, so a redirection test that
        accepts "reported dirty" as a pass cannot see this guard at all. Refusing is
        deliberate: a repository that has written a redirection is a repository whose state
        Claude Away should decline to summarise, whether or not this build happens to be
        immune to it.
        """
        repo = make_repo(tmp_path / "repo")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        repo.git("config", "core.worktree", str(decoy))

        with pytest.raises(UnsafeRepositoryConfigError, match=r"core\.worktree"):
            inspect_repository(repo.path)

    def test_a_submodules_own_core_worktree_is_accepted(self, tmp_path: Path) -> None:
        """The other half: `git submodule add` writes `core.worktree` legitimately.

        Refusing it would refuse every submodule, which is how a safeguard becomes a bug.
        """
        root = TestEveryInvocationIsBound()._superproject(tmp_path)
        module_config = root / ".git" / "modules" / "mod" / "config"
        assert "worktree" in module_config.read_text(encoding="utf-8"), (
            "fixture no longer produces the case under test"
        )
        assert inspect_repository(root).status.is_clean


class TestTheWalkIsBounded:
    def test_more_repositories_than_the_budget_is_refused_not_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The depth cap does not bound the walk; the repository chooses the breadth.

        Depth 8 with a wide fan-out is ``breadth ** depth`` repositories, and none of those
        paths is a cycle, so per-branch cycle detection sees nothing wrong. The flat budget
        is the bound, and it refuses rather than truncating -- stopping early and reporting
        ``clean`` would be a verdict about a subtree nobody walked.
        """
        from claude_away.adapters import git as git_module

        assert git_module._MAX_INSPECTED_REPOSITORIES >= 64, (
            "the real budget must leave room for ordinary multi-submodule projects"
        )
        # Lowered rather than built out to 257 real submodules: the fixture cost would be
        # ~30s and would prove nothing the small case does not.
        monkeypatch.setattr(git_module, "_MAX_INSPECTED_REPOSITORIES", 3)

        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")

        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("init")
        for index in range(3):
            submodule(top.path, source.path, f"mod{index}")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(top.path)
        assert caught.value.details["repository_limit"] == 3

    def test_a_tree_within_the_budget_is_inspected_rather_than_refused(
        self, tmp_path: Path
    ) -> None:
        """The bound must not become a blanket refusal of ordinary multi-submodule projects."""
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")

        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("init")
        for index in range(5):
            submodule(top.path, source.path, f"mod{index}")

        assert inspect_repository(top.path).status.is_clean


class TestRealWorldRepositoriesAreNotRefused:
    """Default-deny is only usable if the "normal" set is genuinely covered."""

    @pytest.mark.parametrize(
        "key,value",
        [
            # husky and friends write this locally in a large share of real projects. It is
            # allowed only because `core.hooksPath` is pinned to /dev/null on every call.
            ("core.hooksPath", ".husky/_"),
            ("commit.gpgsign", "false"),
            ("core.untrackedCache", "true"),
            ("lfs.repositoryformatversion", "0"),
            ("remote.origin.promisor", "true"),
            ("remote.origin.partialclonefilter", "blob:none"),
            ("submodule.mod.ignore", "all"),
            ("branch.main.pushRemote", "origin"),
        ],
    )
    def test_an_ordinary_local_key_is_accepted(self, tmp_path: Path, key: str, value: str) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", key, value)
        assert inspect_repository(repo.path).status.is_clean

    def test_a_hooks_path_does_not_fire_during_inspection(self, tmp_path: Path) -> None:
        """A repository-named hooks directory is never consulted during an inspection.

        Honest scope: this does NOT pin the `-c core.hooksPath=/dev/null` override, and it
        was originally written as though it did. It passes with that pin removed, because
        read-only inspection under `GIT_OPTIONAL_LOCKS=0` never writes the index and so
        never reaches a hook at all. The pin itself is pinned by
        `test_repository_trust.py::test_every_pinned_key_is_actually_passed`, as an argv
        contract. Both facts together are the reason `core.hooksPath` is allow-listed.
        """
        repo = make_repo(tmp_path / "repo")
        marker = tmp_path / "EXECUTED"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        repo.git("config", "core.hooksPath", str(hooks))

        repo.write("f.txt", "changed\n")
        inspect_repository(repo.path)

        assert not marker.exists(), "a repository-named hooks directory was consulted"

    def test_extensions_object_format_is_accepted_at_format_version_one(
        self, tmp_path: Path
    ) -> None:
        """`extensions.objectFormat` is v1-only, so the format version has to move with it.

        The value is `sha1`, not `sha256`: a real sha256 repository cannot be faked with a
        config line, and what needs pinning is that the key is accepted at all.

        Parametrising it alongside the v0 keys made Git itself refuse the fixture, which
        would have hidden whether the allow-list accepts the key at all.
        """
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "core.repositoryFormatVersion", "1")
        repo.git("config", "extensions.objectFormat", "sha1")
        assert inspect_repository(repo.path).status.is_clean


class TestPointerFilesAreParsedExactlyAsGitParsesThem:
    """A pointer parser that disagrees with Git names a git directory Git would never use.

    Found by review of the first version of this module, which used ``.strip()``. Git's
    ``read_gitfile_gently`` trims only ``\\n`` and ``\\r``, so a git directory whose name ends
    in a space -- which ``git init --separate-git-dir`` and ``git submodule add`` both
    produce for such a path -- came back trimmed. With a decoy directory sitting at the
    trimmed name, every bound invocation was pointed at a different repository and the whole
    inspection agreed with itself while disagreeing with Git.

    The same mistake had already been made and fixed once in ``GitRunner.path``.
    """

    def test_a_git_dir_whose_name_ends_in_a_space_is_not_trimmed(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        store = tmp_path / "store "
        git("init", "-q", f"--separate-git-dir={store}", str(worktree))
        pointer = (worktree / ".git").read_bytes().rstrip(b"\n")
        assert pointer.endswith(b" "), f"fixture lost the trailing space: {pointer!r}"

        assert discover_layout(worktree).git_dir == store.resolve()

    def test_a_decoy_at_the_trimmed_name_is_not_inspected(self, tmp_path: Path) -> None:
        """The exploit the trim enabled, end to end."""
        worktree = tmp_path / "wt"
        real = tmp_path / "store "
        git("init", "-q", f"--separate-git-dir={real}", str(worktree))
        git("-C", str(worktree), "config", "user.email", "a@b")
        git("-C", str(worktree), "config", "user.name", "a")
        (worktree / "a.txt").write_text("x", encoding="utf-8")
        git("-C", str(worktree), "add", "-A")
        git("-C", str(worktree), "commit", "-q", "-m", "real")

        decoy = make_repo(tmp_path / "decoysrc")
        decoy.write("d.txt", "decoy\n")
        decoy_head = decoy.commit_all("decoy commit")
        (tmp_path / "store").mkdir()
        for entry in (decoy.path / ".git").iterdir():
            entry.rename(tmp_path / "store" / entry.name)

        inspection = inspect_repository(worktree)
        assert inspection.git_dir == real.resolve()
        assert inspection.head_commit != decoy_head

    def test_a_submodule_path_ending_in_a_space_still_works(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")
        super_repo = make_repo(tmp_path / "super")
        super_repo.write("t.txt", "t")
        super_repo.commit_all("init")
        submodule(super_repo.path, source.path, "sub ")

        assert inspect_repository(super_repo.path).status.is_clean

    def test_a_commondir_ending_in_a_space_is_not_trimmed(self, tmp_path: Path) -> None:
        """`common_dir` is where `local_config` comes from, so trimming moves the audited file."""
        host = make_repo(tmp_path / "host")
        host.write("a.txt", "x")
        host.commit_all("i")
        real = tmp_path / "common "
        (host.path / ".git").rename(real)

        worktree = tmp_path / "wt"
        (worktree / ".git").mkdir(parents=True)
        (worktree / ".git" / "HEAD").write_bytes((real / "HEAD").read_bytes())
        (worktree / ".git" / "commondir").write_bytes(str(real).encode())

        assert discover_layout(worktree).common_dir == real.resolve()

    @pytest.mark.parametrize(
        "content",
        [
            b"gitdir:%s",  # no space after the colon
            b"  gitdir: %s",  # leading whitespace
            b"not a pointer at all",
        ],
    )
    def test_a_pointer_git_itself_rejects_is_rejected_here(
        self, tmp_path: Path, content: bytes
    ) -> None:
        """More permissive than Git means inspecting trees Git does not call repositories."""
        host = make_repo(tmp_path / "host")
        probe = tmp_path / "probe"
        probe.mkdir()
        (probe / ".git").write_bytes(content.replace(b"%s", str(host.path / ".git").encode()))

        assert (
            subprocess.run(
                ["git", "-C", str(probe), "rev-parse", "--absolute-git-dir"],
                capture_output=True,
                timeout=60,
                check=False,
            ).returncode
            != 0
        ), "fixture is not actually rejected by git, so this proves nothing"

        with pytest.raises(UnsupportedRepositoryError):
            discover_layout(probe)


class TestBareRepositoriesAndTheirWorktrees:
    def test_a_bare_repository_is_still_refused(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        git("init", "-q", "--bare", str(bare))
        with pytest.raises(UnsupportedRepositoryError, match="bare repositories"):
            inspect_repository(bare)

    def test_a_linked_worktree_of_a_bare_repository_is_not_called_bare(
        self, tmp_path: Path
    ) -> None:
        """`git clone --bare` + `git worktree add` is ordinary, and the tree is real.

        The bare repository's `core.bare = true` is also the *common* config of every linked
        worktree it hosts, so a check on the value alone refused a working tree with a
        working `git status` -- and the operator could not fix it without breaking Git.
        """
        origin = make_repo(tmp_path / "origin")
        origin.write("a.txt", "x")
        origin.commit_all("first")
        bare = tmp_path / "bare.git"
        git("clone", "-q", "--bare", str(origin.path), str(bare))
        linked = tmp_path / "linked"
        git("-C", str(bare), "worktree", "add", "-q", str(linked), "-b", "work")

        assert inspect_repository(linked).status.is_clean


class TestDiagnosticsCarryNoCredentials:
    def test_a_credential_in_a_key_name_is_redacted(self, tmp_path: Path) -> None:
        """Values were redacted first; keys were not, and that is where the token usually is.

        `url.<base>.insteadOf` with an embedded token is what `actions/checkout` writes, and
        none of those keys is on the allow-list -- so a default-deny audit is *guaranteed* to
        name them in a refusal.
        """
        repo = make_repo(tmp_path / "repo")
        repo.git(
            "config",
            "url.https://x-access-token:ghp_SECRETTOKENINKEY@github.com/.insteadOf",
            "https://github.com/",
        )

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)

        rendered = str(caught.value.to_dict())
        assert "ghp_SECRETTOKENINKEY" not in rendered, rendered
        assert "<redacted>@github.com" in rendered, rendered

    def test_a_credential_in_a_command_bearing_key_is_redacted_too(self, tmp_path: Path) -> None:
        """The other refusal path: `credential.<url>.helper` takes a URL subsection as well."""
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "credential.https://u:ghp_OTHERSECRET@example.invalid.helper", "!true")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert "ghp_OTHERSECRET" not in str(caught.value.to_dict())

    def test_the_refusal_names_the_file_to_edit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "futureClaudeAwayTest.someKey", "value")
        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            inspect_repository(repo.path)
        assert caught.value.details["config"] == str(repo.path / ".git" / "config")

    def test_includes_and_unknown_keys_are_reported_together(self, tmp_path: Path) -> None:
        """One round trip, not two: fixing the include only to be told about the rest is a
        second failing unattended run for no reason."""
        extra = tmp_path / "extra.cfg"
        extra.write_text("[user]\n\tname = x\n", encoding="utf-8")
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "include.path", str(extra))
        repo.git("config", "futureClaudeAwayTest.someKey", "value")

        with pytest.raises(UnsafeRepositoryConfigError) as caught:
            audit_local_config(discover_layout(repo.path))
        keys = [key.lower() for key in caught.value.details["keys"]]
        assert "include.path" in keys
        assert "futureclaudeawaytest.somekey" in keys


class TestTheAllowListCoversRealProjects:
    def test_this_projects_own_repository_is_accepted(self) -> None:
        """The first version of the allow-list refused it, over `gc.auto`.

        A safeguard that refuses the project it ships in is one nobody will leave switched on.
        """
        audit_local_config(discover_layout(Path(__file__).resolve().parents[2]))

    @pytest.mark.parametrize(
        "key,value",
        [
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("maintenance.strategy", "incremental"),
            ("user.signingKey", "ABCD1234"),
            ("tag.gpgsign", "true"),
            ("gpg.format", "ssh"),
            ("fetch.prune", "true"),
            ("pull.ff", "only"),
            ("rebase.autoStash", "true"),
            ("diff.algorithm", "histogram"),
            ("merge.conflictStyle", "zdiff3"),
            ("rerere.enabled", "true"),
            ("push.followTags", "true"),
            ("push.autoSetupRemote", "true"),
            ("feature.manyFiles", "true"),
            ("index.version", "4"),
            ("core.commitGraph", "true"),
            ("core.preloadIndex", "true"),
            ("checkout.workers", "0"),
            ("http.postBuffer", "524288000"),
            ("color.ui", "auto"),
            ("log.date", "iso"),
            ("branch.sort", "-committerdate"),
            ("remote.pushDefault", "origin"),
            ("remote.origin.gh-resolved", "base"),
            ("branch.main.vscode-merge-base", "origin/main"),
            ("gitflow.branch.develop", "develop"),
            ("gitflow.prefix.feature", "feature/"),
            ("lfs.url", "https://example.invalid/lfs"),
        ],
    )
    def test_a_key_ordinary_tooling_writes_is_accepted(
        self, tmp_path: Path, key: str, value: str
    ) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", key, value)
        assert inspect_repository(repo.path).status.is_clean

    @pytest.mark.parametrize(
        "key,value",
        [
            # Each of these decides what `git status` finds or what Git executes, so each
            # stays refused however common it is.
            ("core.excludesFile", "/dev/null"),
            ("core.attributesFile", "/dev/null"),
            ("status.showUntrackedFiles", "no"),
            ("core.fsmonitor", "/bin/true"),
            ("init.defaultBranch", "attacker-branch"),
        ],
    )
    def test_a_masking_key_stays_refused(self, tmp_path: Path, key: str, value: str) -> None:
        repo = make_repo(tmp_path / "repo")
        repo.git("config", key, value)
        with pytest.raises(UnsafeRepositoryConfigError):
            inspect_repository(repo.path)


class TestGitlinkRecordChangesAreNotSuppressed:
    """``--ignore-submodules=all`` was a false-clean regression, found by review.

    ``all`` suppresses two different things: the submodule's *worktree* state, which the
    manual walk reconstructs and improves on, and the gitlink *record* -- the five porcelain
    shapes ``M.``, ``A.``, ``D.``, ``.D`` and ``.T``. The walk cannot reconstruct the second
    group from the child, because they are facts about the *parent's* index and worktree, so
    six ordinary dirty states reported clean while ``git status`` itself printed them.
    ``dirty`` suppresses exactly the first group.

    Each case below asserts against plain ``git status`` first, so a fixture that stops
    reproducing the state fails loudly rather than passing vacuously.
    """

    def _superproject(self, tmp_path: Path) -> tuple[Path, Path]:
        source = make_repo(tmp_path / "src")
        source.write("f.txt", "one\n")
        source.commit_all("one")
        source.write("f.txt", "two\n")
        source.commit_all("two")

        top = make_repo(tmp_path / "top")
        top.write("x.txt", "x")
        top.commit_all("init")
        child = submodule(top.path, source.path, "sub")
        return top.path, child

    def _assert_git_sees_it(self, root: Path, expected: str) -> None:
        raw = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        assert expected in raw, f"fixture no longer produces {expected!r}; git said {raw!r}"

    def test_a_staged_submodule_bump(self, tmp_path: Path) -> None:
        root, child = self._superproject(tmp_path)
        git("-C", str(child), "checkout", "-q", "HEAD~1")
        git("-C", str(root), "add", "sub")
        self._assert_git_sees_it(root, "M  sub")
        assert not inspect_repository(root).status.is_clean

    def test_a_staged_gitlink_delete(self, tmp_path: Path) -> None:
        root, _ = self._superproject(tmp_path)
        git("-C", str(root), "rm", "-q", "--cached", "sub")
        import shutil

        shutil.rmtree(root / "sub")
        self._assert_git_sees_it(root, "D  sub")
        assert not inspect_repository(root).status.is_clean

    def test_a_deleted_submodule_directory(self, tmp_path: Path) -> None:
        root, _ = self._superproject(tmp_path)
        import shutil

        shutil.rmtree(root / "sub")
        self._assert_git_sees_it(root, " D sub")
        assert not inspect_repository(root).status.is_clean

    def test_a_submodule_replaced_by_a_file(self, tmp_path: Path) -> None:
        root, _ = self._superproject(tmp_path)
        import shutil

        shutil.rmtree(root / "sub")
        (root / "sub").write_text("not a submodule\n", encoding="utf-8")
        self._assert_git_sees_it(root, " T sub")
        assert not inspect_repository(root).status.is_clean

    def test_an_unstaged_submodule_bump_is_still_caught(self, tmp_path: Path) -> None:
        root, child = self._superproject(tmp_path)
        git("-C", str(child), "checkout", "-q", "HEAD~1")
        self._assert_git_sees_it(root, " M sub")
        assert not inspect_repository(root).status.is_clean

    def test_a_gitlink_whose_repository_was_removed_but_content_left_behind(
        self, tmp_path: Path
    ) -> None:
        """Git's own porcelain says nothing here at any --ignore-submodules setting.

        Deleting ``sub/.git`` after rewriting ``sub/f.txt`` left the tree reporting clean
        while the swapped content survived ``git checkout -b``. It is reported as
        unverifiable rather than dirty, because that is the accurate statement: there is no
        repository there to ask.
        """
        root, child = self._superproject(tmp_path)
        (child / "f.txt").write_text("SOMEONE ELSE'S WORK\n", encoding="utf-8")
        (child / ".git").unlink()  # a submodule's .git is a gitdir pointer file

        raw = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--ignore-submodules=none"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        assert raw.strip() == "", f"git unexpectedly reports this now: {raw!r}"

        status = inspect_repository(root).status
        assert not status.is_clean
        assert any(entry.startswith("sub") for entry in status.unverifiable), status.unverifiable

    def test_an_uninitialised_submodule_is_still_clean(self, tmp_path: Path) -> None:
        """The case the old comment described, and the reason this is not simply "dirty"."""
        root, child = self._superproject(tmp_path)
        import shutil

        shutil.rmtree(child)
        child.mkdir()
        assert inspect_repository(root).status.is_clean

    def test_a_conflicted_gitlink_is_reported_once(self, tmp_path: Path) -> None:
        """Stages 1/2/3 are one path, not three submodules to walk."""
        root, _ = self._superproject(tmp_path)
        oid = git("-C", str(root), "rev-parse", "HEAD:sub").stdout.strip()
        subprocess.run(
            ["git", "-C", str(root), "update-index", "--index-info"],
            input="".join(f"160000 {oid} {stage}\tsub\n" for stage in (1, 2, 3)),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        status = inspect_repository(root).status
        assert not status.is_clean
        assert [module.path for module in status.submodules].count("sub") <= 1


class TestGuardsThatNoTestReached:
    """Five guards a mutation run proved unpinned: each mutation survived the whole suite.

    Every test here was written from the proof-of-concept that survived, not from the
    guard's source, so it asserts the observable difference rather than the branch.
    """

    def test_a_gitlink_escaping_its_superproject_is_refused(self, tmp_path: Path) -> None:
        """Deleting the containment check let the walk inspect and report on an outside tree."""
        outside = make_repo(tmp_path / "outside")
        outside.write("o.txt", "x")
        outside.commit_all("outside")

        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")
        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("init")
        child = submodule(top.path, source.path, "mod")

        import shutil

        shutil.rmtree(child)
        child.symlink_to(outside.path, target_is_directory=True)

        with pytest.raises(UnsafeRepositoryConfigError, match="outside its superproject"):
            inspect_repository(top.path)

    def test_a_submodule_pointing_at_an_ancestors_git_dir_is_refused(self, tmp_path: Path) -> None:
        """Deleting the cycle check replaced the refusal with a verdict computed while
        re-walking the superproject as its own submodule."""
        source = make_repo(tmp_path / "src")
        source.write("a.txt", "x")
        source.commit_all("inner")
        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("init")
        child = submodule(top.path, source.path, "mod")

        (child / ".git").write_text(f"gitdir: {top.path / '.git'}\n", encoding="utf-8")

        with pytest.raises(UnsafeRepositoryConfigError, match="cycle"):
            inspect_repository(top.path)

    def test_a_submodule_at_a_different_commit_than_recorded_is_dirty(self, tmp_path: Path) -> None:
        """The signal the design gave up `--ignore-submodules=none` to reconstruct by hand.

        With `commit_changed` forced to False the whole suite still passed, so nothing
        exercised it end to end. Asserted here on an otherwise entirely clean tree, so the
        gitlink comparison is the only thing that can produce the verdict.
        """
        source = make_repo(tmp_path / "src")
        source.write("f.txt", "one\n")
        source.commit_all("one")
        top = make_repo(tmp_path / "top")
        top.write("t.txt", "t")
        top.commit_all("init")
        child = submodule(top.path, source.path, "mod")

        git("-C", str(child), "checkout", "-q", "-b", "ahead")
        (child / "f.txt").write_text("two\n", encoding="utf-8")
        git("-C", str(child), "add", "-A")
        git("-C", str(child), "commit", "-q", "-m", "advance")

        status = inspect_repository(top.path).status
        assert not status.is_clean
        assert [module.path for module in status.dirty_submodules] == ["mod"]
        assert next(m for m in status.submodules if m.path == "mod").commit_changed

    def test_a_nested_unverifiable_path_is_named_not_merely_counted(self, tmp_path: Path) -> None:
        """Dropping `nested_unverifiable` left `is_clean` correct by a second route.

        The path vanished from the report while the verdict stayed right, so every existing
        assertion held. An operator reading `awayctl repos --json` to find out *what* is
        unverifiable got an empty list.
        """
        top, deep = TestNestedSubmodules()._chain(tmp_path)
        (deep / "d.txt").write_text("CHANGED\n", encoding="utf-8")
        git("-C", str(deep), "update-index", "--assume-unchanged", "d.txt")

        status = inspect_repository(top).status
        assert "mid/deep/d.txt" in status.unverifiable, status.unverifiable

    def test_core_bare_true_with_a_working_tree_is_refused(self, tmp_path: Path) -> None:
        """Deleting the value validation turned a refusal into `is_clean = True`."""
        repo = make_repo(tmp_path / "repo")
        repo.git("config", "core.bare", "true")
        with pytest.raises(UnsupportedRepositoryError, match="bare repositories"):
            inspect_repository(repo.path)

    def test_an_unsupported_format_version_is_a_typed_refusal(self, tmp_path: Path) -> None:
        """Not an opaque `GitCommandError` from git's own exit 128.

        The nearest existing test asserted only that the error was not
        `NotAGitRepositoryError`, which a `GitCommandError` satisfies -- so the validation
        could be deleted with the suite green.
        """
        repo = make_repo(tmp_path / "repo")
        (repo.path / ".git" / "config").write_text(
            "[core]\n\trepositoryformatversion = 99\n", encoding="utf-8"
        )
        with pytest.raises(UnsupportedRepositoryError, match="format version"):
            inspect_repository(repo.path)
