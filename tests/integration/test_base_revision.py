"""Expected-base resolution: the refusals matter more than the successes."""

from __future__ import annotations

from pathlib import Path

from claude_away.adapters.git import inspect_repository
from claude_away.core.base_revision import BaseRefusal, BaseResolution, resolve_expected_base
from tests.gitfixtures import make_repo


def resolve(
    path: Path,
    *,
    default: str | None = "main",
    expected_commit: str | None = None,
) -> BaseResolution:
    inspection = inspect_repository(path, configured_default_branch=default)
    return resolve_expected_base(inspection, project_id="api", expected_commit=expected_commit)


class TestResolves:
    def test_clean_repository_on_the_default_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        resolution = resolve(repo.path)
        assert resolution.resolved
        assert resolution.commit == repo.head()
        assert resolution.branch == "main"

    def test_matching_expected_commit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        resolution = resolve(repo.path, expected_commit=repo.head())
        assert resolution.resolved


class TestRefusals:
    def test_dirty_worktree(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("README.md", "changed\n")

        resolution = resolve(repo.path)
        assert not resolution.resolved
        assert BaseRefusal.DIRTY_WORKTREE in resolution.refusals

    def test_untracked_file_is_enough_to_refuse(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("scratch.txt", "notes\n")
        assert BaseRefusal.DIRTY_WORKTREE in resolve(repo.path).refusals

    def test_detached_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("second.txt")
        repo.commit_all("second")
        repo.git("checkout", "-q", "--detach", "HEAD")

        assert BaseRefusal.DETACHED_HEAD in resolve(repo.path).refusals

    def test_unexpected_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.git("checkout", "-q", "-b", "somewhere-else")

        resolution = resolve(repo.path)
        assert BaseRefusal.UNEXPECTED_BRANCH in resolution.refusals
        assert resolution.detail["current_branch"] == "somewhere-else"

    def test_missing_expected_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        resolution = resolve(repo.path, default="main")
        assert BaseRefusal.MISSING_REF in resolution.refusals

    def test_unknown_default_branch(self, tmp_path: Path) -> None:
        """No configured default, no origin/HEAD: refuse rather than guess `main`."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        inspection = inspect_repository(repo.path)
        object.__setattr__(inspection, "default_branch", None)

        resolution = resolve_expected_base(inspection, project_id="api")
        assert not resolution.resolved
        assert BaseRefusal.UNKNOWN_DEFAULT_BRANCH in resolution.refusals

    def test_unborn_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r", initial_commit=False)
        resolution = resolve(repo.path)
        assert resolution.refusals == (BaseRefusal.UNBORN_HEAD,)

    def test_diverged_from_the_expected_commit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        stale = repo.head()
        repo.write("moved.txt")
        repo.commit_all("moved on")

        resolution = resolve(repo.path, expected_commit=stale)
        assert BaseRefusal.DIVERGED_FROM_EXPECTED in resolution.refusals
        assert resolution.detail["expected_commit"] == stale

    def test_merge_in_progress(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("shared.txt", "base\n")
        repo.commit_all("base")
        repo.git("checkout", "-q", "-b", "feature")
        repo.write("shared.txt", "feature\n")
        repo.commit_all("feature")
        repo.git("checkout", "-q", "main")
        repo.write("shared.txt", "main\n")
        repo.commit_all("main")
        repo.git("merge", "feature", check=False)

        resolution = resolve(repo.path)
        assert BaseRefusal.OPERATION_IN_PROGRESS in resolution.refusals
        assert BaseRefusal.UNMERGED_PATHS in resolution.refusals

    def test_an_interrupted_cherry_pick_series_refuses(self, tmp_path: Path) -> None:
        """End to end: the sequencer queue must reach a refusal, not just a marker check."""
        repo = make_repo(tmp_path / "r")
        repo.write("f.txt", "1\n")
        repo.commit_all("c0")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("f.txt", "A\n")
        repo.commit_all("cA")
        repo.write("f.txt", "B\n")
        repo.commit_all("cB")
        repo.git("checkout", "-q", "main")
        repo.write("f.txt", "Z\n")
        repo.commit_all("cZ")
        repo.git("cherry-pick", "side~1", "side", check=False)
        # Finished by hand rather than with --continue: CHERRY_PICK_HEAD goes, the queue stays.
        repo.write("f.txt", "A\n")
        repo.git("add", "f.txt")
        repo.git("commit", "-q", "-m", "resolved by hand")

        assert (repo.path / ".git" / "sequencer" / "todo").exists(), "fixture did not stage it"
        resolution = resolve(repo.path)
        assert not resolution.resolved
        assert BaseRefusal.OPERATION_IN_PROGRESS in resolution.refusals

    def test_all_refusals_are_collected_not_short_circuited(self, tmp_path: Path) -> None:
        """An operator fixing one problem should see the rest without another round trip."""
        repo = make_repo(tmp_path / "r")
        repo.write("second.txt")
        repo.commit_all("second")
        repo.git("checkout", "-q", "--detach", "HEAD")
        repo.write("dirty.txt", "x")

        refusals = set(resolve(repo.path).refusals)
        assert {BaseRefusal.DETACHED_HEAD, BaseRefusal.DIRTY_WORKTREE} <= refusals

    def test_refusal_serialises_for_the_supervisor(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("dirty.txt", "x")
        payload = resolve(repo.path).to_dict()
        assert payload["resolved"] is False
        assert "dirty_worktree" in payload["refusals"]
        assert payload["project_id"] == "api"


class TestNoAutomaticRepair:
    def test_a_missing_ref_is_not_fetched(self, tmp_path: Path) -> None:
        """M2A never reaches the network, so a missing ref stays missing."""
        repo = make_repo(tmp_path / "r", initial_branch="trunk")
        before = repo.git("rev-parse", "--verify", "main", check=False).returncode
        resolve(repo.path, default="main")
        after = repo.git("rev-parse", "--verify", "main", check=False).returncode
        assert before != 0 and after != 0

    def test_inspection_does_not_modify_the_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "r")
        repo.write("scratch.txt", "x")
        before = repo.git("status", "--porcelain=v2", "-z").stdout
        head_before = repo.head()

        resolve(repo.path)

        assert repo.git("status", "--porcelain=v2", "-z").stdout == before
        assert repo.head() == head_before
