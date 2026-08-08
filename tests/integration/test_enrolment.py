"""The enrolled-repository boundary.

Every test here is a way of ending up with authority the user did not grant.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_away.core.enrolment import enrol_projects, resolve_config_path
from claude_away.errors import (
    DuplicateEnrolmentError,
    EnrolmentError,
    NotAGitRepositoryError,
    NotEnrolledError,
    UnsafeStateLocationError,
    UnsupportedRepositoryError,
)
from tests.conftest import config_document
from tests.gitfixtures import make_repo


def config_for(
    tmp_path: Path, projects: list[dict[str, Any]], *, state_db: str | None = None
) -> dict[str, Any]:
    document = config_document()
    document["projects"] = projects
    document["stateDbPath"] = state_db or str(tmp_path / "state" / "state.db")
    return document


class TestHappyPath:
    def test_single_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path), "defaultBranch": "main"}]),
            config_dir=tmp_path,
        )
        assert len(enrolment.repositories) == 1
        enrolled = enrolment.by_id("api")
        assert enrolled.root == repo.path.resolve()
        assert enrolled.default_branch == "main"

    def test_multiple_repositories(self, tmp_path: Path) -> None:
        first = make_repo(tmp_path / "api")
        second = make_repo(tmp_path / "web")
        enrolment = enrol_projects(
            config_for(
                tmp_path,
                [
                    {"id": "api", "path": str(first.path)},
                    {"id": "web", "path": str(second.path)},
                ],
            ),
            config_dir=tmp_path,
        )
        assert {r.project_id for r in enrolment.repositories} == {"api", "web"}

    def test_relative_path_resolves_against_the_config_directory(self, tmp_path: Path) -> None:
        """Never the process cwd: a `cd` must not change which repository is enrolled."""
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": "api"}]), config_dir=tmp_path
        )
        assert enrolment.by_id("api").root == repo.path.resolve()


class TestPathRejection:
    def test_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(EnrolmentError, match="does not exist"):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(tmp_path / "nope")}]),
                config_dir=tmp_path,
            )

    def test_file_where_a_directory_is_required(self, tmp_path: Path) -> None:
        target = tmp_path / "a-file"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(EnrolmentError, match="not a directory"):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(target)}]), config_dir=tmp_path
            )

    def test_directory_that_is_not_a_repository(self, tmp_path: Path) -> None:
        """Recorded as a failure, not raised: it disables that project, not the run."""
        plain = tmp_path / "plain"
        plain.mkdir()
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(plain)}]), config_dir=tmp_path
        )
        assert enrolment.repositories == ()
        assert isinstance(enrolment.failures[0].error, NotAGitRepositoryError)
        with pytest.raises(NotEnrolledError):
            enrolment.by_id("api")

    def test_bare_repository_is_refused(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True, timeout=60
        )
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(bare)}]), config_dir=tmp_path
        )
        assert enrolment.repositories == ()
        error = enrolment.failures[0].error
        assert isinstance(error, UnsupportedRepositoryError)
        assert "bare" in error.message

    def test_subdirectory_does_not_silently_widen_to_the_repository(self, tmp_path: Path) -> None:
        """The scope-widening case.

        Enrolling `repo/src` must not quietly enrol `repo`. The user named a directory;
        accepting it as the whole repository grants authority over everything beside it.
        """
        repo = make_repo(tmp_path / "api")
        subdirectory = repo.path / "src"
        subdirectory.mkdir()

        with pytest.raises(EnrolmentError, match="subdirectory of a repository"):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(subdirectory)}]),
                config_dir=tmp_path,
            )

    def test_nested_repository_enrols_as_itself(self, tmp_path: Path) -> None:
        """A repository inside another is its own root, not the parent's."""
        outer = make_repo(tmp_path / "outer")
        inner = make_repo(outer.path / "nested")

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "inner", "path": str(inner.path)}]),
            config_dir=tmp_path,
        )
        assert enrolment.by_id("inner").root == inner.path.resolve()
        assert enrolment.by_id("inner").root != outer.path.resolve()


class TestSymlinksAndDuplicates:
    def test_symlinked_path_is_canonicalised(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        link = tmp_path / "api-link"
        link.symlink_to(repo.path, target_is_directory=True)

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(link)}]), config_dir=tmp_path
        )
        assert enrolment.by_id("api").root == repo.path.resolve()

    def test_two_ids_for_one_repository_via_symlink_are_refused(self, tmp_path: Path) -> None:
        """Locks and branch names are keyed by project id; two ids for one tree is a lie."""
        repo = make_repo(tmp_path / "api")
        link = tmp_path / "api-link"
        link.symlink_to(repo.path, target_is_directory=True)

        with pytest.raises(DuplicateEnrolmentError) as caught:
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {"id": "api", "path": str(repo.path)},
                        {"id": "api-alias", "path": str(link)},
                    ],
                ),
                config_dir=tmp_path,
            )
        assert caught.value.details["project_ids"] == ["api", "api-alias"]

    def test_two_ids_via_dot_segments_are_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        indirect = tmp_path / "api" / ".." / "api"

        with pytest.raises(DuplicateEnrolmentError):
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {"id": "api", "path": str(repo.path)},
                        {"id": "api-again", "path": str(indirect)},
                    ],
                ),
                config_dir=tmp_path,
            )

    def test_trailing_slash_is_the_same_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        with pytest.raises(DuplicateEnrolmentError):
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {"id": "api", "path": str(repo.path)},
                        {"id": "api-slash", "path": str(repo.path) + "/"},
                    ],
                ),
                config_dir=tmp_path,
            )


class TestStateDatabaseLocation:
    def test_state_db_inside_an_enrolled_repository_is_refused(self, tmp_path: Path) -> None:
        """The ledger that judges the work must not sit where the work happens."""
        repo = make_repo(tmp_path / "api")
        with pytest.raises(UnsafeStateLocationError) as caught:
            enrol_projects(
                config_for(
                    tmp_path,
                    [{"id": "api", "path": str(repo.path)}],
                    state_db=str(repo.path / ".claude-away" / "state.db"),
                ),
                config_dir=tmp_path,
            )
        assert caught.value.details["repository_root"] == str(repo.path.resolve())

    def test_state_db_equal_to_the_repository_root_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        with pytest.raises(UnsafeStateLocationError):
            enrol_projects(
                config_for(
                    tmp_path, [{"id": "api", "path": str(repo.path)}], state_db=str(repo.path)
                ),
                config_dir=tmp_path,
            )

    def test_state_db_reaching_in_via_dot_segments_is_refused(self, tmp_path: Path) -> None:
        """`outside/../api/state.db` is inside the repository however it is spelled."""
        repo = make_repo(tmp_path / "api")
        sneaky = tmp_path / "outside" / ".." / "api" / "state.db"
        with pytest.raises(UnsafeStateLocationError):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(repo.path)}], state_db=str(sneaky)),
                config_dir=tmp_path,
            )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_state_db_reaching_in_via_symlink_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        inside = repo.path / "statedir"
        inside.mkdir()
        link = tmp_path / "looks-outside"
        link.symlink_to(inside, target_is_directory=True)

        with pytest.raises(UnsafeStateLocationError):
            enrol_projects(
                config_for(
                    tmp_path,
                    [{"id": "api", "path": str(repo.path)}],
                    state_db=str(link / "state.db"),
                ),
                config_dir=tmp_path,
            )

    def test_state_db_outside_every_repository_is_accepted(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(
                tmp_path,
                [{"id": "api", "path": str(repo.path)}],
                state_db=str(tmp_path / "elsewhere" / "state.db"),
            ),
            config_dir=tmp_path,
        )
        assert len(enrolment.repositories) == 1


class TestAuthorisation:
    def test_unknown_project_id_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError):
            enrolment.by_id("web")

    def test_unenrolled_path_is_refused(self, tmp_path: Path) -> None:
        """Discovering a repository is not the same as being allowed to touch it."""
        enrolled = make_repo(tmp_path / "api")
        stranger = make_repo(tmp_path / "someone-elses-project")

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(enrolled.path)}]),
            config_dir=tmp_path,
        )
        with pytest.raises(NotEnrolledError):
            enrolment.require_path(stranger.path)

    def test_parent_directory_is_not_authorised(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError):
            enrolment.require_path(tmp_path)

    def test_path_inside_an_enrolled_repository_is_authorised(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        (repo.path / "src").mkdir()
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        assert enrolment.require_path(repo.path / "src").project_id == "api"

    def test_escaping_via_dot_segments_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        stranger = make_repo(tmp_path / "stranger")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError):
            enrolment.require_path(repo.path / ".." / "stranger")
        assert stranger.path.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_symlink_out_of_an_enrolled_repository_is_refused(self, tmp_path: Path) -> None:
        """A symlink inside the repo pointing elsewhere does not extend authority."""
        repo = make_repo(tmp_path / "api")
        stranger = make_repo(tmp_path / "stranger")
        escape = repo.path / "escape-hatch"
        escape.symlink_to(stranger.path, target_is_directory=True)

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError):
            enrolment.require_path(escape)


class TestConfigPathResolution:
    def test_absolute_path_is_unchanged(self, tmp_path: Path) -> None:
        assert resolve_config_path(str(tmp_path), base_dir=Path("/elsewhere")) == tmp_path.resolve()

    def test_relative_path_uses_the_base_directory(self, tmp_path: Path) -> None:
        assert (
            resolve_config_path("sub/file.db", base_dir=tmp_path)
            == (tmp_path / "sub" / "file.db").resolve()
        )

    def test_nonexistent_path_still_resolves(self, tmp_path: Path) -> None:
        """The state database does not exist before the first run."""
        resolved = resolve_config_path("state/new.db", base_dir=tmp_path)
        assert resolved == (tmp_path / "state" / "new.db").resolve()


class TestModeGuard:
    def test_cloud_mode_is_not_enrolled_by_path(self, tmp_path: Path) -> None:
        document = config_document(mode="cloud")
        document["projects"] = [{"id": "api", "repository": "https://example.invalid/api.git"}]
        with pytest.raises(EnrolmentError) as caught:
            enrol_projects(document, config_dir=tmp_path)

        # Asserting on the exact message, not `match="local-mode"`: with the mode guard
        # deleted the cloud project falls through to "local-mode project has no path",
        # which also contains that substring, so the test passed either way.
        assert caught.value.details["mode"] == "cloud"
        assert "only local-mode enrolment" in caught.value.message

    def test_a_cloud_project_that_also_has_a_path_is_still_refused(self, tmp_path: Path) -> None:
        """The guard is about the mode, not about the project happening to lack a path."""
        repo = make_repo(tmp_path / "api")
        document = config_document(mode="cloud")
        document["projects"] = [{"id": "api", "path": str(repo.path)}]
        with pytest.raises(EnrolmentError) as caught:
            enrol_projects(document, config_dir=tmp_path)
        assert caught.value.details["mode"] == "cloud"


class TestConfiguredDefaultBranch:
    """`defaultBranch` is operator-supplied text that reaches Git as an argument.

    `--upload-pack=...` is a legal branch name as far as the config schema is concerned, so
    the value is checked against the ref guard at enrolment rather than being carried until
    something deep in the Git adapter refuses it.
    """

    @pytest.mark.parametrize(
        "branch",
        [
            "--upload-pack=touch /tmp/claude-away-should-not-exist",
            "-c",
            "--exec=id",
            "main..other",
            "main^{}",
            "with space",
            "with\ttab",
        ],
    )
    def test_option_shaped_default_branch_is_refused(self, tmp_path: Path, branch: str) -> None:
        repo = make_repo(tmp_path / "api")
        with pytest.raises(EnrolmentError, match="defaultBranch"):
            enrol_projects(
                config_for(
                    tmp_path, [{"id": "api", "path": str(repo.path), "defaultBranch": branch}]
                ),
                config_dir=tmp_path,
            )

    def test_the_refusal_happens_before_git_is_asked_anything(self, tmp_path: Path) -> None:
        """The payload must never reach a `git` argv, so nothing it names may appear."""
        marker = tmp_path / "PWNED"
        repo = make_repo(tmp_path / "api")
        with pytest.raises(EnrolmentError):
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {
                            "id": "api",
                            "path": str(repo.path),
                            "defaultBranch": f"--upload-pack=touch {marker}",
                        }
                    ],
                ),
                config_dir=tmp_path,
            )
        assert not marker.exists()

    def test_an_ordinary_branch_name_is_accepted(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(
                tmp_path,
                [{"id": "api", "path": str(repo.path), "defaultBranch": "release/2026-08"}],
            ),
            config_dir=tmp_path,
        )
        assert enrolment.by_id("api").default_branch == "release/2026-08"

    def test_an_absent_default_branch_is_not_an_error(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]),
            config_dir=tmp_path,
        )
        assert enrolment.by_id("api").default_branch in {None, "main"}


class TestNestedEnrolmentIsResolvedBySpecificity:
    """Two legitimately enrolled repositories can nest. Which one owns a path?"""

    def _nested(self, tmp_path: Path) -> tuple[Path, Path]:
        outer = make_repo(tmp_path / "outer")
        inner = make_repo(outer.path / "vendor" / "inner")
        return outer.path, inner.path

    @pytest.mark.parametrize("reverse", [False, True])
    def test_the_innermost_repository_wins_regardless_of_config_order(
        self, tmp_path: Path, reverse: bool
    ) -> None:
        """The answer must not depend on how the configuration file happens to be written.

        Returning the first match made this order-dependent: the same path on the same
        filesystem was authorised as `outer` or as `inner` depending on which entry came
        first. Locks, leases and branch names are keyed by project id, so that is two
        different exclusive claims over one tree.
        """
        outer, inner = self._nested(tmp_path)
        projects = [
            {"id": "outer", "path": str(outer)},
            {"id": "inner", "path": str(inner)},
        ]
        if reverse:
            projects.reverse()

        enrolment = enrol_projects(config_for(tmp_path, projects), config_dir=tmp_path)
        assert enrolment.require_path(inner / "file.txt").project_id == "inner"

    def test_a_path_outside_the_inner_repository_still_belongs_to_the_outer(
        self, tmp_path: Path
    ) -> None:
        outer, inner = self._nested(tmp_path)
        enrolment = enrol_projects(
            config_for(
                tmp_path,
                [{"id": "outer", "path": str(outer)}, {"id": "inner", "path": str(inner)}],
            ),
            config_dir=tmp_path,
        )
        assert enrolment.require_path(outer / "src" / "app.py").project_id == "outer"


class TestGitDirectoryIsNeverAuthorised:
    """`.git/config` names commands Git executes; writing it chooses what we run."""

    @pytest.mark.parametrize(
        "relative",
        [".git", ".git/config", ".git/hooks/pre-commit", ".git/objects/ab/cdef"],
    )
    def test_paths_inside_the_git_directory_are_refused(
        self, tmp_path: Path, relative: str
    ) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError, match="git directory"):
            enrolment.require_path(repo.path / relative)

    def test_an_ordinary_path_is_still_authorised(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        assert enrolment.require_path(repo.path / "src" / "app.py").project_id == "api"

    def test_a_file_merely_named_gitignore_is_not_the_git_directory(self, tmp_path: Path) -> None:
        """Component-wise, not prefix: `.gitignore` is an ordinary tracked file."""
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        assert enrolment.require_path(repo.path / ".gitignore").project_id == "api"


class TestLinkedWorktrees:
    def test_two_linked_worktrees_of_one_repository_are_refused(self, tmp_path: Path) -> None:
        """Different roots, one ref namespace.

        `git worktree add` gives a repository a second working tree: same objects, same
        refs, same config. Two project ids over that share a branch namespace, so a branch
        created for one task is the same branch the other can move -- and a checkout of a
        branch already checked out in the sibling fails outright.
        """
        main = make_repo(tmp_path / "main")
        linked = tmp_path / "linked"
        main.git("worktree", "add", "-q", str(linked), "-b", "side")

        with pytest.raises(DuplicateEnrolmentError, match="linked worktrees"):
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {"id": "main", "path": str(main.path)},
                        {"id": "side", "path": str(linked)},
                    ],
                ),
                config_dir=tmp_path,
            )

    def test_either_worktree_alone_enrols_normally(self, tmp_path: Path) -> None:
        main = make_repo(tmp_path / "main")
        linked = tmp_path / "linked"
        main.git("worktree", "add", "-q", str(linked), "-b", "side")

        for project_id, path in (("main", main.path), ("side", linked)):
            enrolment = enrol_projects(
                config_for(tmp_path, [{"id": project_id, "path": str(path)}]),
                config_dir=tmp_path,
            )
            assert enrolment.by_id(project_id).root == path.resolve()

    def test_two_genuinely_separate_repositories_are_unaffected(self, tmp_path: Path) -> None:
        first = make_repo(tmp_path / "api")
        second = make_repo(tmp_path / "web")
        enrolment = enrol_projects(
            config_for(
                tmp_path,
                [{"id": "api", "path": str(first.path)}, {"id": "web", "path": str(second.path)}],
            ),
            config_dir=tmp_path,
        )
        assert len(enrolment.repositories) == 2


class TestStateDbBoundaryUsesFilesystemIdentity:
    """String comparison alone leaves the boundary open on a case-insensitive filesystem.

    `Path.resolve` is case-preserving on POSIX and `is_relative_to` is exact-case, so on
    macOS a stateDbPath spelled `~/projects/myapp/...` against an enrolled root of
    `~/Projects/MyApp` names the same directory while comparing as unrelated -- and the
    ledger lands inside the repository it judges. That filesystem cannot be created here,
    so the identity path is exercised through a spelling difference Linux does have.
    """

    def test_a_differently_spelled_root_still_triggers_the_guard(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _is_inside

        real = tmp_path / "repo"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)

        candidate = real / ".claude-away" / "state.db"
        assert not candidate.exists()
        # The string test cannot see it: neither path is a prefix of the other.
        assert not candidate.is_relative_to(alias)
        assert _is_inside(candidate, alias)

    def test_an_unrelated_path_is_still_allowed(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _is_inside

        repo = tmp_path / "repo"
        repo.mkdir()
        assert not _is_inside(tmp_path / "elsewhere" / "state.db", repo)

    def test_a_root_that_does_not_exist_does_not_crash(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _is_inside

        assert not _is_inside(tmp_path / "a" / "b.db", tmp_path / "gone")


class TestTheGitDirectoryGuardUsesTheRealGitDirectory:
    """The guard checked the literal name `.git`, which is a different question."""

    def test_a_relocated_git_directory_is_still_refused(self, tmp_path: Path) -> None:
        """Self-disarming otherwise: two file operations remove the constraint.

        Git does not require the directory to be called `.git` or to sit at the root. An
        agent with ordinary write access to the working tree -- exactly what M2B grants --
        could `mv .git .store` plus a `gitdir:` pointer, and the guard that stops it choosing
        what the controller executes would permit writing config and hooks again.
        """
        repo = make_repo(tmp_path / "api")
        real = repo.path / ".git"
        moved = repo.path / ".store"
        real.rename(moved)
        (repo.path / ".git").write_text(f"gitdir: {moved}\n", encoding="utf-8")

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        with pytest.raises(NotEnrolledError, match="git directory"):
            enrolment.require_path(moved / "config")

    def test_a_nested_repositorys_git_directory_is_refused(self, tmp_path: Path) -> None:
        """Only the FIRST component was compared, so `vendor/lib/.git/config` sailed through."""
        outer = make_repo(tmp_path / "outer")
        make_repo(outer.path / "vendor" / "lib")

        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "outer", "path": str(outer.path)}]), config_dir=tmp_path
        )
        for relative in (
            "vendor/lib/.git",
            "vendor/lib/.git/config",
            "vendor/lib/.git/hooks/pre-commit",
        ):
            with pytest.raises(NotEnrolledError, match="git directory"):
                enrolment.require_path(outer.path / relative)

    def test_ordinary_nested_files_are_still_authorised(self, tmp_path: Path) -> None:
        outer = make_repo(tmp_path / "outer")
        (outer.path / "vendor").mkdir()
        assert (
            enrol_projects(
                config_for(tmp_path, [{"id": "outer", "path": str(outer.path)}]),
                config_dir=tmp_path,
            )
            .require_path(outer.path / "vendor" / "notes.md")
            .project_id
            == "outer"
        )


class TestDuplicateDetectionUsesFilesystemIdentity:
    def test_two_spellings_of_one_tree_are_refused(self, tmp_path: Path) -> None:
        """`by_root` was a dict lookup, so only identical strings collided.

        The comment beside it claimed to cover "a different mount path". `Path.resolve`
        collapses symlinks and `..` -- a mount point is neither, so two bind mounts of one
        working tree enrolled as two projects over one ref namespace. Bind mounts cannot be
        created here; a symlinked *root* passed unresolved exercises the same comparison.
        """
        from claude_away.core.enrolment import _lookup

        real = tmp_path / "repo"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)

        seen = {alias: "first"}
        assert alias not in {real}  # the string comparison genuinely misses
        assert _lookup(seen, real) == "first"

    def test_an_unrelated_path_is_not_a_duplicate(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _lookup

        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        assert _lookup({first: "one"}, second) is None

    def test_a_missing_path_does_not_crash_the_lookup(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _lookup

        assert _lookup({tmp_path / "gone": "x"}, tmp_path / "also-gone") is None


class TestEnrolmentKeepsItsInspections:
    def test_the_inspection_is_available_without_a_second_one(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "api")
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(repo.path)}]), config_dir=tmp_path
        )
        assert enrolment.inspections["api"].root == repo.path.resolve()

    def test_a_failed_project_has_no_inspection(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        enrolment = enrol_projects(
            config_for(tmp_path, [{"id": "api", "path": str(plain)}]), config_dir=tmp_path
        )
        assert "api" not in enrolment.inspections


class TestBothDuplicateGuardsAreExercised:
    """Four duplicate tests all also shared a git-common-dir, so only one guard was pinned."""

    def test_the_root_guard_fires_for_two_spellings_of_one_worktree(self, tmp_path: Path) -> None:
        from claude_away.core.enrolment import _lookup

        repo = make_repo(tmp_path / "api")
        seen = {repo.path.resolve(): "first"}
        assert _lookup(seen, repo.path.resolve()) == "first"

    def test_the_error_names_the_root_when_roots_collide(self, tmp_path: Path) -> None:
        """`repository_root` in the details is what distinguishes it from the worktree guard."""
        repo = make_repo(tmp_path / "api")
        link = tmp_path / "alias"
        link.symlink_to(repo.path, target_is_directory=True)

        with pytest.raises(DuplicateEnrolmentError) as caught:
            enrol_projects(
                config_for(
                    tmp_path,
                    [
                        {"id": "api", "path": str(repo.path)},
                        {"id": "alias", "path": str(link)},
                    ],
                ),
                config_dir=tmp_path,
            )
        assert "repository_root" in caught.value.details
        assert "same repository" in caught.value.message


class TestFailedProjectsStillClaimTheirPath:
    def test_the_state_db_cannot_hide_in_a_project_that_failed_to_enrol(
        self, tmp_path: Path
    ) -> None:
        """A repository being unreadable today is no reason to let the ledger sit inside it.

        The docstring said so; the single line implementing it (`claimed_paths.append` on
        the failure branch) had no test, so deleting it was silent.
        """
        broken = make_repo(tmp_path / "broken")
        broken.git("config", "filter.evil.clean", "/bin/true")

        with pytest.raises(UnsafeStateLocationError):
            enrol_projects(
                config_for(
                    tmp_path,
                    [{"id": "broken", "path": str(broken.path)}],
                    state_db=str(broken.path / "state.db"),
                ),
                config_dir=tmp_path,
            )
