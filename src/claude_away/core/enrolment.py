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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_away.adapters.git import RepositoryInspection, inspect_repository, is_safe_ref
from claude_away.errors import (
    ClaudeAwayError,
    DuplicateEnrolmentError,
    EnrolmentError,
    GitError,
    NotAGitRepositoryError,
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

    git_dir: Path
    """Where Git actually keeps this repository's data. Usually ``<root>/.git`` -- but not
    always, and the difference is a security boundary rather than trivia."""

    common_dir: Path
    """The shared object/ref store. Differs from ``git_dir`` only for a linked worktree."""

    default_branch: str | None
    """From configuration, or discovered locally. ``None`` means genuinely unknown."""

    discovered_default_branch: str | None
    """What the repository itself says, from ``refs/remotes/origin/HEAD``.

    Carried even when the operator declared a different branch, so the protected set can be
    the union of both and a decoy can only add protection rather than move it.
    """

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
            "git_dir": str(self.git_dir),
            "default_branch": self.default_branch,
            "default_branch_source": self.default_branch_source,
            "discovered_default_branch": self.discovered_default_branch,
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
    inspections: Mapping[str, RepositoryInspection] = field(default_factory=dict)
    """The inspection each repository was enrolled from, keyed by project id.

    Kept so that a caller wanting to describe a repository does not have to inspect it a
    second time. ``awayctl repos`` used to, passing the enrolment's *resolved* default
    branch back in through the parameter reserved for the operator's declaration -- so a
    value read out of ``refs/remotes/origin/HEAD`` came back stamped ``configured``, and the
    JSON carried two contradictory provenance claims for one branch. Re-inspecting also
    doubled the Git work, and every inspection is a chance to execute something.
    """

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

        That refusal used to compare the first path component against the literal string
        ``.git``, which is not the same question and missed two real cases. Git does not
        require the directory to be called ``.git`` or to live at the root
        (``git init --separate-git-dir``, and the ``gitdir:`` pointer file that submodules
        and linked worktrees use), so a repository could simply move it -- and an agent with
        ordinary write access to the working tree can do that with two file operations,
        disarming the guard that constrains it. Nested repositories missed too: only the
        *first* component was compared, so ``<root>/vendor/lib/.git/config`` sailed through.

        The check now uses the real git directory and common directory, which inspection
        already computed, and refuses a ``.git`` component anywhere in the path rather than
        only at the front.
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

        # Every enrolled repository's stores, not just `best`'s. A git directory that is
        # neither named `.git` nor belongs to the longest match was authorised:
        # `git init --separate-git-dir=<root>/store` puts a live git directory at
        # `<root>/store`, and with two enrolled repositories one project's store can sit
        # inside another's tree. The `.git`-component fallback only catches the literal name.
        stores = [
            store
            for repository in self.repositories
            for store in (repository.git_dir, repository.common_dir)
        ]
        for store in stores:
            if candidate == store or candidate.is_relative_to(store):
                raise NotEnrolledError(
                    "paths inside the git directory are never authorised; its config names "
                    "commands that Git executes, so writing there is equivalent to choosing "
                    "what the controller runs",
                    path=str(candidate),
                    project_id=best.project_id,
                    git_dir=str(store),
                )

        # A git directory need not be called `.git`. `git init --separate-git-dir=<root>/store`
        # puts a live one at an arbitrary name, and it may belong to a nested repository that
        # is not enrolled at all, so neither the enrolled-store list above nor the `.git`
        # name test below sees it. Detect the directory by its contents instead: a git dir
        # always holds HEAD, objects/ and refs/. Filesystem probing only -- no Git invoked,
        # so nothing here can execute repository-chosen configuration.
        probe = candidate if candidate.is_dir() else candidate.parent
        while probe == best.root or probe.is_relative_to(best.root):
            if (
                (probe / "HEAD").is_file()
                and (probe / "objects").is_dir()
                and (probe / "refs").is_dir()
            ):
                raise NotEnrolledError(
                    "paths inside a git directory are never authorised; this one is not "
                    "named .git but has a git directory's contents",
                    path=str(candidate),
                    project_id=best.project_id,
                    git_dir=str(probe),
                )
            if probe == best.root:
                break
            probe = probe.parent

        relative = candidate.relative_to(best.root)
        if ".git" in relative.parts:
            # Covers a nested repository's own git directory, which is not this project's
            # `git_dir` and would otherwise be authorised under the enclosing project.
            raise NotEnrolledError(
                "paths inside a git directory are never authorised, including one belonging "
                "to a repository nested inside this one",
                path=str(candidate),
                project_id=best.project_id,
            )
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"repositories": [r.to_dict() for r in self.repositories]}


def _enclosing_repository_root(path: Path) -> Path | None:
    """The nearest ancestor that holds a ``.git`` entry, or ``None``.

    A pure filesystem walk. Asking Git would mean asking a repository where it thinks its
    own root is, which is the vote this whole boundary exists to withdraw; here the answer
    is only used to phrase a refusal, but a diagnostic the repository can steer is a
    diagnostic that misdirects the operator.
    """
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return None


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
    try:
        inspection = inspector(resolved, configured_default_branch=project.get("defaultBranch"))
    except NotAGitRepositoryError as exc:
        # The Git adapter refuses any path that is not a repository root, because resolving
        # the root through Git would let the repository's own `core.worktree` choose it. That
        # refusal is correct but says only "not a repository", so the far more likely cause
        # is diagnosed here -- from the filesystem, never from Git -- and named.
        # Only when the enrolled path has no `.git` entry of its own. A path that *does*
        # have one and still failed is a repository with something wrong inside it -- a
        # dangling gitdir pointer, an unreadable `.git` -- and calling that "a subdirectory
        # of <parent>" sends the operator to enrol the wrong root.
        enclosing = None if (resolved / ".git").exists() else _enclosing_repository_root(resolved)
        if enclosing is None:
            raise
        raise EnrolmentError(
            "configured path is a subdirectory of a repository, not its root; enrol the "
            "repository root explicitly if that is what you meant",
            project_id=project_id,
            configured_path=str(resolved),
            repository_root=str(enclosing),
        ) from exc

    if inspection.root != resolved:
        # Defence in depth: the adapter now returns the enrolled path itself, so this cannot
        # fire. It stays because the alternative to noticing a mismatch is enrolling a scope
        # the user never named.
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
            git_dir=inspection.git_dir,
            common_dir=inspection.common_dir,
            default_branch=inspection.default_branch,
            default_branch_source=inspection.default_branch_source,
            discovered_default_branch=inspection.discovered_default_branch,
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
    inspections: dict[str, RepositoryInspection] = {}
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

        existing = _lookup(by_root, repository.root)
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
        sharing = _lookup(by_store, inspection.common_dir)
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
        inspections[repository.project_id] = inspection
        enrolled.append(repository)

    _assert_state_db_outside_repositories(config, config_dir=config_dir, claimed=claimed_paths)

    return Enrolment(
        repositories=tuple(enrolled), failures=tuple(failures), inspections=inspections
    )


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


def _lookup(seen: Mapping[Path, str], candidate: Path) -> str | None:
    """Find an already-seen path that is the same directory as ``candidate``.

    A plain dict lookup compares strings, and the comment beside the duplicate check claimed
    to cover "a different mount path" -- which it did not. Two bind mounts of one working
    tree are two distinct canonical paths: `Path.resolve` collapses symlinks and `..`, but
    a mount point is neither, so both keys missed and both projects enrolled. That is two
    project ids over one tree and one ref namespace, which is precisely what
    :class:`~claude_away.errors.DuplicateEnrolmentError` exists to prevent. The same gap
    covers a case-insensitive filesystem, where two spellings name one directory.

    The dict lookup stays as the fast path; `samefile` settles the rest by asking the
    filesystem instead of comparing text.
    """
    direct = seen.get(candidate)
    if direct is not None:
        return direct
    for path, project_id in seen.items():
        try:
            if path.exists() and candidate.exists() and path.samefile(candidate):
                return project_id
        except OSError:  # pragma: no cover - racing filesystem
            continue
    return None


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
