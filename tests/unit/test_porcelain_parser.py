"""The porcelain v2 parser, driven with synthetic bytes.

Real repositories cover the behaviour that Git can be persuaded to produce. This file
covers the behaviour it should never produce -- truncated records, unknown record types,
malformed fields -- because the parser's contract is that it *refuses* such input rather
than silently dropping the entry. A dropped entry means a dirty repository reported as
clean, and "clean" is the answer that gets somebody else's work committed over.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_away.adapters.git import (
    GitRunner,
    RepositoryOperation,
    _operations_in_progress,
    _parse_porcelain_v2,
    _resolve_default_branch,
)
from claude_away.errors import GitOutputError
from tests.gitfixtures import make_repo


def record(*parts: bytes) -> bytes:
    return b"\x00".join(parts) + b"\x00"


class TestWellFormedInput:
    def test_empty_output_is_a_clean_tree(self) -> None:
        status = _parse_porcelain_v2(b"")
        assert status.is_clean

    def test_headers_are_ignored(self) -> None:
        status = _parse_porcelain_v2(
            record(b"# branch.oid abc123", b"# branch.head main", b"# branch.ab +0 -0")
        )
        assert status.is_clean

    def test_ignored_entries_are_ignored(self) -> None:
        assert _parse_porcelain_v2(record(b"! build/output.o")).is_clean

    def test_staged_and_unstaged_flags(self) -> None:
        status = _parse_porcelain_v2(
            record(
                b"1 M. N... 100644 100644 100644 aaa bbb staged-only.txt",
                b"1 .M N... 100644 100644 100644 aaa bbb unstaged-only.txt",
                b"1 MM N... 100644 100644 100644 aaa bbb both.txt",
            )
        )
        assert status.staged == ("staged-only.txt", "both.txt")
        assert status.unstaged == ("unstaged-only.txt", "both.txt")

    def test_a_rename_consumes_its_original_path(self) -> None:
        """The cursor must skip the original path, not read it as a new record."""
        status = _parse_porcelain_v2(
            record(
                b"2 R. N... 100644 100644 100644 aaa bbb R100 new-name.txt",
                b"old-name.txt",
                b"? sentinel.txt",
            )
        )
        assert status.staged == ("new-name.txt",)
        # If the original path had been mis-read as an entry, the sentinel would be lost
        # or "old-name.txt" would appear as untracked.
        assert status.untracked == ("sentinel.txt",)

    def test_unmerged_entry(self) -> None:
        status = _parse_porcelain_v2(
            record(b"u UU N... 100644 100644 100644 100644 aaa bbb ccc conflicted.txt")
        )
        assert status.unmerged == ("conflicted.txt",)
        assert not status.is_clean

    def test_paths_with_spaces_survive_field_splitting(self) -> None:
        """Splitting is bounded, so a path containing spaces is not chopped up."""
        status = _parse_porcelain_v2(
            record(b"1 .M N... 100644 100644 100644 aaa bbb dir with spaces/file name.txt")
        )
        assert status.unstaged == ("dir with spaces/file name.txt",)

    def test_invalid_utf8_path_round_trips(self) -> None:
        """A repository must not become uninspectable because one filename is odd bytes."""
        payload = record(b"? bad-\xff-name.txt")
        status = _parse_porcelain_v2(payload)
        assert len(status.untracked) == 1
        assert status.untracked[0].startswith("bad-")


class TestSubmoduleField:
    def test_dirty_submodule_flags(self) -> None:
        status = _parse_porcelain_v2(record(b"1 .M SCMU 160000 160000 160000 aaa bbb vendor/lib"))
        assert len(status.submodules) == 1
        module = status.submodules[0]
        assert module.commit_changed and module.has_modifications and module.has_untracked
        assert module.is_dirty
        assert not status.is_clean

    def test_clean_submodule_is_not_dirty(self) -> None:
        status = _parse_porcelain_v2(record(b"1 .M S... 160000 160000 160000 aaa bbb vendor/lib"))
        assert status.submodules[0].is_dirty is False

    def test_malformed_submodule_field_is_refused(self) -> None:
        with pytest.raises(GitOutputError, match="submodule field"):
            _parse_porcelain_v2(record(b"1 .M SCM 160000 160000 160000 aaa bbb vendor/lib"))


class TestMalformedInputIsRefused:
    def test_unknown_record_type(self) -> None:
        with pytest.raises(GitOutputError, match="unrecognised"):
            _parse_porcelain_v2(record(b"z something entirely new"))

    def test_truncated_ordinary_entry(self) -> None:
        with pytest.raises(GitOutputError, match="unparseable"):
            _parse_porcelain_v2(record(b"1 .M N... 100644"))

    def test_truncated_unmerged_entry(self) -> None:
        with pytest.raises(GitOutputError, match="unparseable unmerged"):
            _parse_porcelain_v2(record(b"u UU N... 100644 100644"))

    def test_rename_missing_its_original_path(self) -> None:
        payload = b"2 R. N... 100644 100644 100644 aaa bbb R100 new-name.txt\x00"
        with pytest.raises(GitOutputError, match="missing its original path"):
            _parse_porcelain_v2(payload)

    def test_malformed_status_code(self) -> None:
        with pytest.raises(GitOutputError, match="malformed status code"):
            _parse_porcelain_v2(record(b"1 XYZ N... 100644 100644 100644 aaa bbb file.txt"))


class TestOperationDetection:
    def test_no_markers_means_no_operation(self, tmp_path: Path) -> None:
        (tmp_path / "gitdir").mkdir()
        assert _operations_in_progress(tmp_path / "gitdir") == ()

    def test_rebase_marker(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "gitdir"
        (git_dir / "rebase-merge").mkdir(parents=True)
        assert _operations_in_progress(git_dir) == (RepositoryOperation.REBASE,)

    def test_apply_mailbox_supersedes_rebase(self, tmp_path: Path) -> None:
        """`git am` shares rebase-apply with rebase; the applying marker disambiguates."""
        git_dir = tmp_path / "gitdir"
        (git_dir / "rebase-apply").mkdir(parents=True)
        (git_dir / "rebase-apply" / "applying").write_text("", encoding="utf-8")

        operations = _operations_in_progress(git_dir)
        assert operations == (RepositoryOperation.APPLY_MAILBOX,)

    def test_sequencer_todo_is_an_operation_in_progress(self, tmp_path: Path) -> None:
        """CHERRY_PICK_HEAD is removed before the sequencer queue is.

        `git cherry-pick A B` that conflicts on A and is finished with a plain `git commit`
        rather than `--continue` leaves sequencer/todo holding the remaining picks. `git
        status` still says "Cherry-pick currently in progress" and `--continue` still works,
        while the marker this used to look for is already gone -- so a repository mid-series
        reported no operation and resolved a base.
        """
        git_dir = tmp_path / "gitdir"
        (git_dir / "sequencer").mkdir(parents=True)
        (git_dir / "sequencer" / "todo").write_text("pick abc123\n", encoding="utf-8")

        assert _operations_in_progress(git_dir) == (RepositoryOperation.CHERRY_PICK,)

    def test_sequencer_opts_distinguishes_revert(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "gitdir"
        (git_dir / "sequencer").mkdir(parents=True)
        (git_dir / "sequencer" / "todo").write_text("revert abc123\n", encoding="utf-8")
        (git_dir / "sequencer" / "opts").write_text('"--revert"\n', encoding="utf-8")

        assert _operations_in_progress(git_dir) == (RepositoryOperation.REVERT,)

    def test_an_empty_sequencer_directory_is_not_an_operation(self, tmp_path: Path) -> None:
        """`git cherry-pick --quit` leaves the directory; only `todo` means "in progress"."""
        git_dir = tmp_path / "gitdir"
        (git_dir / "sequencer").mkdir(parents=True)
        assert _operations_in_progress(git_dir) == ()

    def test_revert_and_bisect(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "gitdir"
        git_dir.mkdir()
        (git_dir / "REVERT_HEAD").write_text("", encoding="utf-8")
        (git_dir / "BISECT_LOG").write_text("", encoding="utf-8")

        operations = set(_operations_in_progress(git_dir))
        assert operations == {RepositoryOperation.REVERT, RepositoryOperation.BISECT}


class TestDefaultBranchResolution:
    def test_configuration_wins_without_consulting_git(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        runner = GitRunner(repo.path)
        assert _resolve_default_branch(runner, "explicit") == ("explicit", "configured", None)

    def test_origin_head_is_used_when_present(self, tmp_path: Path) -> None:
        """Set locally by a clone. Reading it is not a network call."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        repo.git("update-ref", "refs/remotes/origin/trunk", repo.head())
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        assert _resolve_default_branch(GitRunner(repo.path), None) == (
            "trunk",
            "origin_head",
            "trunk",
        )

    def test_init_default_branch_is_never_consulted(self, tmp_path: Path) -> None:
        """It names what `git init` calls a *new* branch, not this repository's default.

        It was the third fallback until review pointed out two problems with it: it is a
        personal preference in ~/.gitconfig with no relationship to an existing repository,
        and `git config --get` reads the merged config, so a line in the repository's own
        .git/config could move protection off the real default branch.
        """
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        repo.git("config", "init.defaultBranch", "development")
        assert _resolve_default_branch(GitRunner(repo.path), None) == (None, None, None)

    def test_nothing_configured_yields_none_not_a_guess(self, tmp_path: Path) -> None:
        """The whole point: never invent "main"."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        assert _resolve_default_branch(GitRunner(repo.path), None) == (None, None, None)

    def test_an_option_shaped_discovered_branch_is_discarded(self, tmp_path: Path) -> None:
        """origin/HEAD is a file anything with repository access can write."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        origin = repo.path / ".git" / "refs" / "remotes" / "origin"
        origin.mkdir(parents=True)
        (origin / "HEAD").write_text(
            "ref: refs/remotes/origin/--upload-pack=id\n", encoding="utf-8"
        )
        assert _resolve_default_branch(GitRunner(repo.path), None) == (None, None, None)


class TestDiscoveryIsAlwaysComputed:
    def test_a_declared_branch_does_not_hide_what_the_repository_says(self, tmp_path: Path) -> None:
        """The union `_protected_branches` documents was impossible to compute before this.

        `_resolve_default_branch` returned the configured value the moment there was one and
        never looked at origin/HEAD, so the protected set was {declared} or {discovered} and
        never both -- and for an undeclared project a decoy MOVED protection rather than
        adding to it.
        """
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        repo.git("update-ref", "refs/remotes/origin/decoy", repo.head())
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/decoy")

        effective, source, discovered = _resolve_default_branch(GitRunner(repo.path), "trunk")
        assert (effective, source) == ("trunk", "configured")
        assert discovered == "decoy", "the repository's own claim must still be visible"
