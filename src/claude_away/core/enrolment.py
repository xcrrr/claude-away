"""Turning configured projects into repositories Claude Away is allowed to touch.

Enrolment is the boundary between "a path exists on this machine" and "the user explicitly
authorised work here". Everything downstream -- locks, branches, commits -- is keyed off an
:class:`EnrolledRepository`, so a path that never made it through this module can never be
mutated by a later milestone.

The module fails closed. Every ambiguity below is refused with an actionable error rather
than resolved by a guess:

* a configured path that resolves to a *subdirectory* of a repository, which would
  silently widen scope from the directory the user named to a parent they did not;
* two project ids that resolve to the same canonical repository, which would let two tasks
  each believe they held exclusive access to one working tree;
* a bare repository, which has no working tree to inspect or build in;
* a state database inside an enrolled repository, where an agent working in that repo
  could reach the very ledger that judges it.

Canonicalisation goes through :meth:`Path.resolve`, so symlinks, ``..`` segments and
platform quirks like macOS's ``/tmp`` -> ``/private/tmp`` all collapse to one spelling
before anything is compared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_away.adapters.git import RepositoryInspection, inspect_repository
from claude_away.errors import (
    DuplicateEnrolmentError,
    EnrolmentError,
    NotEnrolledError,
    UnsafeStateLocationError,
)

__all__ = [
    "EnrolledRepository",
    "Enrolment",
    "enrol_projects",
    "resolve_config_path",
]


def resolve_config_path(raw: str, *, base_dir: Path) -> Path:
    """Resolve a path from configuration.

    Relative paths resolve against the *config file's* directory, never the process working
    directory. A ``cd`` before launching the supervisor must not be able to change which
    repository -- or which state database -- the configuration refers to.
    """
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    # strict=False: the state database legitimately does not exist yet on first run.
    return candidate.resolve()


@dataclass(frozen=True, slots=True)
class EnrolledRepository:
    """A repository the user explicitly authorised, with its canonical identity fixed."""

    project_id: str
    configured_path: Path
    """Exactly what the configuration said, for error messages."""

    root: Path
    """The canonical working-tree root. The identity everything downstream keys off."""

    default_branch: str | None
    """From configuration, or discovered locally. ``None`` means genuinely unknown."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "configured_path": str(self.configured_path),
            "root": str(self.root),
            "default_branch": self.default_branch,
        }


@dataclass(frozen=True, slots=True)
class Enrolment:
    """The complete set of authorised repositories for a configuration."""

    repositories: tuple[EnrolledRepository, ...]

    def by_id(self, project_id: str) -> EnrolledRepository:
        for repository in self.repositories:
            if repository.project_id == project_id:
                return repository
        raise NotEnrolledError(
            "no project with this id is enrolled",
            project_id=project_id,
            enrolled=[r.project_id for r in self.repositories],
        )

    def require_path(self, path: Path | str) -> EnrolledRepository:
        """Authorise a filesystem path, or refuse.

        The reverse lookup a future runner uses before touching anything. A path *inside*
        an enrolled repository authorises to that repository; a path anywhere else --
        including a parent directory or a sibling checkout -- is refused. Discovering a
        repository is not the same as being allowed to use it.
        """
        candidate = Path(path).expanduser().resolve()
        for repository in self.repositories:
            if candidate == repository.root or candidate.is_relative_to(repository.root):
                return repository
        raise NotEnrolledError(
            "path is not inside any enrolled repository",
            path=str(candidate),
            enrolled=[str(r.root) for r in self.repositories],
        )

    def to_dict(self) -> dict[str, Any]:
        return {"repositories": [r.to_dict() for r in self.repositories]}


def _enrol_one(
    project: Mapping[str, Any], *, base_dir: Path, inspector: Any
) -> tuple[EnrolledRepository, RepositoryInspection]:
    project_id = str(project["id"])
    raw_path = project.get("path")
    if not raw_path:
        raise EnrolmentError("local-mode project has no path", project_id=project_id)

    configured = Path(str(raw_path)).expanduser()
    resolved = resolve_config_path(str(raw_path), base_dir=base_dir)

    if not resolved.exists():
        raise EnrolmentError(
            "configured project path does not exist",
            project_id=project_id,
            path=str(resolved),
            configured=str(configured),
        )
    if not resolved.is_dir():
        raise EnrolmentError(
            "configured project path is not a directory",
            project_id=project_id,
            path=str(resolved),
        )

    # Raises NotAGitRepositoryError / UnsupportedRepositoryError (bare) as appropriate.
    inspection = inspector(resolved, configured_default_branch=project.get("defaultBranch"))

    if inspection.root != resolved:
        # The configured path is inside a repository rather than being its root. Accepting
        # it would silently enrol the parent -- a scope the user never named, and possibly
        # a much larger one than they intended.
        raise EnrolmentError(
            "configured path is a subdirectory of a repository, not its root; enrol the "
            "repository root explicitly if that is what you meant",
            project_id=project_id,
            configured_path=str(resolved),
            repository_root=str(inspection.root),
        )

    return (
        EnrolledRepository(
            project_id=project_id,
            configured_path=configured,
            root=inspection.root,
            default_branch=inspection.default_branch,
        ),
        inspection,
    )


def enrol_projects(
    config: Mapping[str, Any],
    *,
    config_dir: Path,
    inspector: Any = inspect_repository,
) -> Enrolment:
    """Build the authorised repository set from a validated configuration.

    ``config`` must already have passed
    :func:`claude_away.core.validation.validate_config_document`; this adds the checks that
    need the filesystem, which a schema cannot perform.

    ``inspector`` is injectable purely so tests can drive failure modes that are awkward to
    stage on disk. The default is the real read-only Git inspection.
    """
    if str(config.get("mode")) != "local":
        raise EnrolmentError(
            "only local-mode enrolment is implemented; cloud repositories are attached by "
            "the routine, not by path",
            mode=str(config.get("mode")),
        )

    projects: Sequence[Mapping[str, Any]] = config.get("projects", ())
    enrolled: list[EnrolledRepository] = []
    by_root: dict[Path, str] = {}

    for project in projects:
        repository, _inspection = _enrol_one(project, base_dir=config_dir, inspector=inspector)

        existing = by_root.get(repository.root)
        if existing is not None:
            # Different spellings -- a symlink, a `..` segment, a different mount path --
            # of one working tree. Locks and branch names are keyed by project id, so two
            # ids for one tree would let two tasks each think they had it exclusively.
            raise DuplicateEnrolmentError(
                "two projects resolve to the same repository",
                repository_root=str(repository.root),
                project_ids=sorted([existing, repository.project_id]),
            )
        by_root[repository.root] = repository.project_id
        enrolled.append(repository)

    _assert_state_db_outside_repositories(config, config_dir=config_dir, enrolled=enrolled)

    return Enrolment(repositories=tuple(enrolled))


def _assert_state_db_outside_repositories(
    config: Mapping[str, Any], *, config_dir: Path, enrolled: Sequence[EnrolledRepository]
) -> None:
    """The state database must not live inside a repository Claude Away works in.

    It is the ledger that decides whether work is DONE. An agent editing files in an
    enrolled repository must not be able to reach it -- not because we expect a model to
    attack it, but because "the judge is inside the thing being judged" is the kind of
    arrangement that only has to fail once.
    """
    raw = config.get("stateDbPath")
    if not raw:
        return

    state_path = resolve_config_path(str(raw), base_dir=config_dir)
    for repository in enrolled:
        if state_path == repository.root or state_path.is_relative_to(repository.root):
            raise UnsafeStateLocationError(
                "the state database would live inside an enrolled repository; move it "
                "outside every enrolled repository",
                state_db_path=str(state_path),
                project_id=repository.project_id,
                repository_root=str(repository.root),
            )
