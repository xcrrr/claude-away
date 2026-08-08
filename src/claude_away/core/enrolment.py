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

from claude_away.adapters.git import RepositoryInspection, inspect_repository, is_safe_ref
from claude_away.errors import (
    ClaudeAwayError,
    DuplicateEnrolmentError,
    EnrolmentError,
    GitError,
    NotEnrolledError,
    UnsafeStateLocationError,
)

__all__ = [
    "EnrolledRepository",
    "Enrolment",
    "EnrolmentFailure",
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

    default_branch_source: str | None
    """Which of those it was: ``"configured"`` or ``"origin_head"``.

    Kept separate because only the first is the operator speaking. ``origin_head`` is read
    out of the repository, and the repository is the thing being supervised -- a decoy
    written there could otherwise move protection off the branch that matters.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "configured_path": str(self.configured_path),
            "root": str(self.root),
            "default_branch": self.default_branch,
            "default_branch_source": self.default_branch_source,
        }


@dataclass(frozen=True, slots=True)
class EnrolmentFailure:
    """A configured project whose repository could not be read.

    Recorded rather than raised so that one unreadable repository does not decide the fate
    of every other one. It is *not* enrolled -- it appears nowhere in
    :attr:`Enrolment.repositories`, so it grants no authority and nothing can be done in it
    -- but a supervisor reading the report can see the other repositories and get on with
    them, and can see why this one is out of action.
    """

    project_id: str
    configured_path: Path
    error: ClaudeAwayError

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "configured_path": str(self.configured_path),
            "error": self.error.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Enrolment:
    """The complete set of authorised repositories for a configuration."""

    repositories: tuple[EnrolledRepository, ...]
    failures: tuple[EnrolmentFailure, ...] = ()

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

        **The most specific repository wins.** Two legitimately enrolled repositories can
        nest -- a vendored checkout, an ``examples/`` project, a submodule working tree --
        and returning the first match made the answer depend on the order of ``projects``
        in the configuration file. Since locks, leases and branch names are keyed by
        project id, that meant a path could be attributed to the outer project while the
        inner project believed it held the same tree exclusively: the exact confusion
        :class:`~claude_away.errors.DuplicateEnrolmentError` exists to prevent, arriving
        through the back door. Longest matching root is the only answer that does not
        depend on how the file happens to be written.

        Paths inside the git directory are never authorised. Nothing Claude Away does needs
        to write there, and ``.git/config`` in particular is *executable configuration*:
        being able to write it means being able to choose a command that the deterministic
        controller then runs on its next inspection. Refusing it here closes that loop from
        the other side, so the guard does not rest on the Git adapter alone.
        """
        candidate = Path(path).expanduser().resolve()

        best: EnrolledRepository | None = None
        for repository in self.repositories:
            if candidate != repository.root and not candidate.is_relative_to(repository.root):
                continue
            if best is None or len(repository.root.parts) > len(best.root.parts):
                best = repository

        if best is None:
            raise NotEnrolledError(
                "path is not inside any enrolled repository",
                path=str(candidate),
                enrolled=[str(r.root) for r in self.repositories],
            )

        relative = candidate.relative_to(best.root)
        if relative.parts and relative.parts[0] == ".git":
            raise NotEnrolledError(
                "paths inside the git directory are never authorised; .git/config names "
                "commands that Git executes, so writing there is equivalent to choosing "
                "what the controller runs",
                path=str(candidate),
                project_id=best.project_id,
            )
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"repositories": [r.to_dict() for r in self.repositories]}


def _enrol_one(
    project: Mapping[str, Any], *, base_dir: Path, inspector: Any
) -> tuple[EnrolledRepository, RepositoryInspection]:
    project_id = str(project["id"])
    raw_path = project.get("path")
    if not raw_path:
        raise EnrolmentError("local-mode project has no path", project_id=project_id)

    configured_branch = project.get("defaultBranch")
    if configured_branch is not None and not is_safe_ref(str(configured_branch)):
        # Caught here so the operator gets an actionable configuration error rather than a
        # Git-layer refusal from somewhere much deeper. The ref guard downstream still
        # holds either way; this is about the message, not the safety.
        raise EnrolmentError(
            "configured defaultBranch is not a usable Git ref name",
            project_id=project_id,
            default_branch=str(configured_branch),
        )

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
            default_branch_source=inspection.default_branch_source,
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
    failures: list[EnrolmentFailure] = []
    by_root: dict[Path, str] = {}
    by_store: dict[Path, str] = {}
    # Failed projects still count for the state-database check below: not being enrollable
    # is no reason to let the ledger sit inside them.
    claimed_paths: list[Path] = []

    for project in projects:
        project_id = str(project.get("id", "<unnamed>"))
        try:
            repository, inspection = _enrol_one(project, base_dir=config_dir, inspector=inspector)
        except GitError as exc:
            # The repository is in a state we cannot read: not a working tree, bare, or
            # carrying configuration that names commands we refuse to run. That is a fact
            # about the world rather than a mistake in the configuration, so it disables
            # this project and leaves the rest of the run intact. Configuration mistakes
            # (EnrolmentError, DuplicateEnrolmentError) still stop everything, because the
            # operator has to fix those before any of it means what they think it means.
            raw = project.get("path")
            configured = resolve_config_path(str(raw), base_dir=config_dir) if raw else config_dir
            failures.append(
                EnrolmentFailure(project_id=project_id, configured_path=configured, error=exc)
            )
            claimed_paths.append(configured)
            continue

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

        # Distinct roots are not enough. `git worktree add` gives one repository a second
        # working tree: different root, same refs, same objects, same config. Two project
        # ids over that share a branch namespace, so a branch created for one task is
        # visible and movable by the other, and `git checkout` of a branch already checked
        # out in the sibling fails outright. The shared store is what identifies them.
        sharing = by_store.get(inspection.common_dir)
        if sharing is not None:
            raise DuplicateEnrolmentError(
                "two projects are linked worktrees of one repository; they share refs and "
                "objects, so a branch made for one is the same branch for the other",
                git_common_dir=str(inspection.common_dir),
                project_ids=sorted([sharing, repository.project_id]),
            )

        by_root[repository.root] = repository.project_id
        by_store[inspection.common_dir] = repository.project_id
        claimed_paths.append(repository.root)
        enrolled.append(repository)

    _assert_state_db_outside_repositories(config, config_dir=config_dir, claimed=claimed_paths)

    return Enrolment(repositories=tuple(enrolled), failures=tuple(failures))


def _assert_state_db_outside_repositories(
    config: Mapping[str, Any], *, config_dir: Path, claimed: Sequence[Path]
) -> None:
    """The state database must not live inside a repository Claude Away works in.

    It is the ledger that decides whether work is DONE. An agent editing files in an
    enrolled repository must not be able to reach it -- not because we expect a model to
    attack it, but because "the judge is inside the thing being judged" is the kind of
    arrangement that only has to fail once.

    ``claimed`` covers every path the configuration named, including projects that failed
    to enrol. A repository being unreadable today is no reason to let the ledger sit inside
    it: the operator will fix the repository, and then it *is* an enrolled repository with
    the state database in it.
    """
    raw = config.get("stateDbPath")
    if not raw:
        return

    state_path = resolve_config_path(str(raw), base_dir=config_dir)
    for root in claimed:
        if _is_inside(state_path, root):
            raise UnsafeStateLocationError(
                "the state database would live inside a configured repository; move it "
                "outside every repository named in the configuration",
                state_db_path=str(state_path),
                repository_root=str(root),
            )


def _is_inside(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is ``root`` or lies beneath it, by identity where possible.

    String comparison alone is not enough, and the gap fails *open*. ``Path.resolve`` is
    case-preserving on POSIX, and ``is_relative_to`` is exact-case, so on macOS's default
    case-insensitive filesystem a ``stateDbPath`` spelled ``~/projects/myapp/...`` against
    an enrolled root of ``~/Projects/MyApp`` names the same directory on disk while
    comparing as unrelated -- and the ledger lands inside the repository it judges.

    The string comparison is kept as the first test because it works for paths that do not
    exist yet, which the state database usually does not. The identity comparison then
    walks up from the nearest ancestor that *does* exist and asks the filesystem, which
    settles case-folding, hard links and any other spelling the operator might use.
    """
    if candidate == root or candidate.is_relative_to(root):
        return True
    if not root.exists():
        return False

    probe = candidate
    while True:
        if probe.exists():
            try:
                if probe.samefile(root):
                    return True
            except OSError:  # pragma: no cover - racing filesystem
                return False
        if probe.parent == probe:
            return False
        probe = probe.parent
