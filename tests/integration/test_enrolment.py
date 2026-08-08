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
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(NotAGitRepositoryError):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(plain)}]), config_dir=tmp_path
            )

    def test_bare_repository_is_refused(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True, timeout=60
        )
        with pytest.raises(UnsupportedRepositoryError, match="bare"):
            enrol_projects(
                config_for(tmp_path, [{"id": "api", "path": str(bare)}]), config_dir=tmp_path
            )

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
        assert caught.value.details["project_id"] == "api"

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
        with pytest.raises(EnrolmentError, match="local-mode"):
            enrol_projects(document, config_dir=tmp_path)


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
