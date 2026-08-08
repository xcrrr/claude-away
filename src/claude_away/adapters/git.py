"""Read-only Git inspection.

Milestone 2A does not mutate repositories. Nothing here creates a branch, writes a commit,
touches the index, or reaches the network. The point is narrower and comes first: before
Claude Away is ever allowed to change a repository, the deterministic controller has to be
able to *state* what that repository currently is.

Four rules shape the implementation:

**argv, never a shell.** Every invocation is a list passed to :func:`subprocess.run` with
no ``shell=True`` anywhere. A branch name is data, and data that reaches a shell is an
injection waiting for the first repository with a ``$(...)`` in a ref name.

**Refs are not options.** Git has no universal way to say "this argument is definitely not
a flag", so a ref beginning with ``-`` is rejected before it reaches argv and ``--`` is used
to end option parsing wherever Git supports it. Otherwise a branch literally named
``--upload-pack=evil`` is an argument the porcelain will happily honour.

**A sanitised environment.** ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_CONFIG`` and friends
silently redirect Git at a different repository than the one in ``-C``. They are stripped,
along with anything that could make Git prompt or open a network connection.

**Refuse rather than guess.** Output this build cannot parse raises
:class:`~claude_away.errors.GitOutputError`. A status parser that skips an entry it does
not recognise reports a dirty repository as clean, and "clean" is the answer that gets
somebody else's uncommitted work built on top of.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from claude_away.errors import (
    GitCommandError,
    GitOutputError,
    NotAGitRepositoryError,
    UnsupportedRepositoryError,
)

__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "GitRunner",
    "RepositoryInspection",
    "RepositoryOperation",
    "SubmoduleState",
    "WorktreeStatus",
    "inspect_repository",
    "is_safe_ref",
    "resolve_local_ref",
]

GIT_TIMEOUT_SECONDS = 60
"""Every Git call is bounded. An unattended run must not hang forever on a stuck lock."""

_MAX_STDERR_CHARS = 2_000

# Environment variables that redirect Git at a different repository, a different config, or
# an interactive/network path. Cleared for every invocation so that inspection describes the
# repository we were pointed at and nothing else.
_STRIPPED_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_ALTERNATE_REFS",
    "GIT_EXTERNAL_DIFF",
    "GIT_PAGER",
    "GIT_EDITOR",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)

_FORCED_ENV = {
    # Never prompt: an unattended run that blocks on a credential prompt is a hang.
    "GIT_TERMINAL_PROMPT": "0",
    # Inspection must not take the index lock; a concurrent human `git status` would
    # otherwise be able to make our read fail, and vice versa.
    "GIT_OPTIONAL_LOCKS": "0",
    # Stable, parseable diagnostics regardless of the operator's locale.
    "LC_ALL": "C",
    "LANG": "C",
}


class RepositoryOperation(str, Enum):
    """A Git operation already in progress in the working tree.

    Detected from marker files in the git directory. Each one means somebody -- or some
    earlier crashed run -- left the repository mid-surgery, and starting new work on top
    would interleave with it.
    """

    MERGE = "merge"
    REBASE = "rebase"
    CHERRY_PICK = "cherry_pick"
    REVERT = "revert"
    BISECT = "bisect"
    APPLY_MAILBOX = "apply_mailbox"


@dataclass(frozen=True, slots=True)
class SubmoduleState:
    """How a submodule diverges from what the superproject records."""

    path: str
    commit_changed: bool
    has_modifications: bool
    has_untracked: bool

    @property
    def is_dirty(self) -> bool:
        return self.commit_changed or self.has_modifications or self.has_untracked


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    """The parsed result of ``git status --porcelain=v2 -z``."""

    staged: tuple[str, ...] = ()
    unstaged: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    unmerged: tuple[str, ...] = ()
    submodules: tuple[SubmoduleState, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Clean means *nothing* would be carried into a new branch.

        Untracked files count. They are the ones most likely to be accidentally added by a
        broad ``git add``, and a task that starts on top of them cannot say afterwards
        which changes were its own.
        """
        return not (
            self.staged or self.unstaged or self.untracked or self.unmerged or self.dirty_submodules
        )

    @property
    def dirty_submodules(self) -> tuple[SubmoduleState, ...]:
        return tuple(module for module in self.submodules if module.is_dirty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.is_clean,
            "staged": list(self.staged),
            "unstaged": list(self.unstaged),
            "untracked": list(self.untracked),
            "unmerged": list(self.unmerged),
            "dirty_submodules": [module.path for module in self.dirty_submodules],
        }


@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    """Everything M2A can say about a repository without touching it."""

    root: Path
    git_dir: Path
    head_commit: str | None
    """``None`` on an unborn branch -- a fresh ``git init`` with no commit yet."""

    branch: str | None
    """``None`` when HEAD is detached."""

    is_detached: bool
    status: WorktreeStatus
    operations_in_progress: tuple[RepositoryOperation, ...]
    default_branch: str | None
    remotes: tuple[str, ...]

    @property
    def is_unborn(self) -> bool:
        return self.head_commit is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "head_commit": self.head_commit,
            "branch": self.branch,
            "detached": self.is_detached,
            "unborn": self.is_unborn,
            "default_branch": self.default_branch,
            "remotes": list(self.remotes),
            "operations_in_progress": [op.value for op in self.operations_in_progress],
            "status": self.status.to_dict(),
        }


def is_safe_ref(ref: str) -> bool:
    """Whether ``ref`` may be passed to Git as data.

    Conservative on purpose. Git itself accepts a wider set than this, but the cost of
    rejecting an unusual-but-legal ref is an actionable error, while the cost of accepting
    an option-shaped one is Git interpreting our data as a flag.
    """
    if not ref or len(ref) > 255:
        return False
    if ref.startswith("-"):
        return False  # option injection: `--upload-pack=...` is a legal branch name
    if ref.startswith("/") or ref.endswith("/") or ref.endswith("."):
        return False
    if any(character in ref for character in ("..", "@{", "\\", "^", ":", "?", "*", "[", "~")):
        return False
    # Control characters, space and DEL are forbidden in ref names by git-check-ref-format.
    return all(not (ord(character) < 0x20 or ord(character) == 0x7F) for character in ref) and (
        " " not in ref
    )


class GitRunner:
    """Runs read-only Git commands against one working tree."""

    def __init__(self, cwd: Path, *, timeout: int = GIT_TIMEOUT_SECONDS) -> None:
        self.cwd = cwd
        self.timeout = timeout

    def _environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV}
        environment.update(_FORCED_ENV)
        return environment

    def run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Invoke Git with the given arguments.

        ``-C`` rather than ``cwd=`` so the target is explicit in the recorded argv, and
        ``--no-optional-locks`` so inspection never contends with a human's shell.
        """
        for argument in arguments:
            if "\x00" in argument:
                raise GitOutputError("git argument contains a NUL byte", argument=argument)

        argv = ["git", "-C", str(self.cwd), "--no-optional-locks", *arguments]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                env=self._environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitCommandError(argv, 127, "git executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(argv, 124, f"git timed out after {self.timeout}s") from exc

        if check and completed.returncode != 0:
            raise GitCommandError(
                argv,
                completed.returncode,
                completed.stderr.decode("utf-8", "replace")[:_MAX_STDERR_CHARS],
                cwd=str(self.cwd),
            )
        return completed

    def text(self, *arguments: str, check: bool = True) -> str:
        completed = self.run(*arguments, check=check)
        return completed.stdout.decode("utf-8", "replace").strip()


def _decode(raw: bytes) -> str:
    """Decode a path from Git.

    ``surrogateescape`` so a filename that is not valid UTF-8 round-trips instead of
    raising. Refusing to inspect a repository because one file has an odd byte sequence
    would be a denial of service on a legitimate project.
    """
    return raw.decode("utf-8", "surrogateescape")


def _parse_porcelain_v2(payload: bytes) -> WorktreeStatus:
    """Parse ``git status --porcelain=v2 -z`` output.

    The subtlety that breaks naive parsers: with ``-z`` a rename/copy entry (``2``) is
    followed by its original path in the *next* NUL-delimited field. A parser that treats
    every field as one entry desynchronises from that point on and misreports everything
    after the first rename. Records are consumed with an explicit cursor for that reason.

    Paths are not unquoted here because ``-z`` output is never quoted -- that is the whole
    reason for using it. Filenames containing spaces, tabs, newlines, quotes, leading
    dashes or invalid UTF-8 all survive intact.
    """
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    unmerged: list[str] = []
    submodules: list[SubmoduleState] = []

    records = payload.split(b"\x00")
    if records and records[-1] == b"":
        records.pop()

    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue

        kind = record[:1]

        if kind == b"#":
            continue  # branch/oid headers

        if kind == b"?":
            untracked.append(_decode(record[2:]))
            continue

        if kind == b"!":
            continue  # ignored; not our business

        if kind in (b"1", b"2"):
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>  then NUL <origPath>
            fields = record.split(b" ", 9 if kind == b"2" else 8)
            expected = 10 if kind == b"2" else 9
            if len(fields) < expected:
                raise GitOutputError("unparseable porcelain v2 entry", record=_decode(record[:200]))
            xy = fields[1].decode("ascii", "replace")
            sub = fields[2].decode("ascii", "replace")
            path = _decode(fields[expected - 1])

            if kind == b"2":
                if index >= len(records):
                    raise GitOutputError(
                        "rename entry missing its original path",
                        record=_decode(record[:200]),
                    )
                index += 1  # consume the original path; it is not a separate entry

            if len(xy) != 2:
                raise GitOutputError("malformed status code", code=xy)
            if xy[0] != ".":
                staged.append(path)
            if xy[1] != ".":
                unstaged.append(path)

            if sub.startswith("S"):
                if len(sub) != 4:
                    raise GitOutputError("malformed submodule field", field=sub)
                submodules.append(
                    SubmoduleState(
                        path=path,
                        commit_changed=sub[1] == "C",
                        has_modifications=sub[2] == "M",
                        has_untracked=sub[3] == "U",
                    )
                )
            continue

        if kind == b"u":
            # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            fields = record.split(b" ", 10)
            if len(fields) < 11:
                raise GitOutputError("unparseable unmerged entry", record=_decode(record[:200]))
            unmerged.append(_decode(fields[10]))
            continue

        raise GitOutputError("unrecognised porcelain v2 record type", record=_decode(record[:200]))

    return WorktreeStatus(
        staged=tuple(staged),
        unstaged=tuple(unstaged),
        untracked=tuple(untracked),
        unmerged=tuple(unmerged),
        submodules=tuple(submodules),
    )


def _operations_in_progress(git_dir: Path) -> tuple[RepositoryOperation, ...]:
    """Detect an interrupted Git operation from marker files.

    Marker files rather than porcelain because there is no single command that reports all
    of these, and because the markers are what Git itself consults.
    """
    found: list[RepositoryOperation] = []
    markers: tuple[tuple[str, RepositoryOperation], ...] = (
        ("MERGE_HEAD", RepositoryOperation.MERGE),
        ("rebase-merge", RepositoryOperation.REBASE),
        ("rebase-apply", RepositoryOperation.REBASE),
        ("CHERRY_PICK_HEAD", RepositoryOperation.CHERRY_PICK),
        ("REVERT_HEAD", RepositoryOperation.REVERT),
        ("BISECT_LOG", RepositoryOperation.BISECT),
    )
    for name, operation in markers:
        if (git_dir / name).exists() and operation not in found:
            found.append(operation)

    # `git am` shares rebase-apply with `git rebase`; the applypatch marker disambiguates.
    if (git_dir / "rebase-apply" / "applying").exists():
        if RepositoryOperation.REBASE in found:
            found.remove(RepositoryOperation.REBASE)
        found.append(RepositoryOperation.APPLY_MAILBOX)

    return tuple(found)


def _resolve_default_branch(runner: GitRunner, configured: str | None) -> str | None:
    """Determine the repository's default branch without touching the network.

    Order: the operator's explicit configuration, then ``refs/remotes/origin/HEAD`` if a
    previous clone recorded one, then ``init.defaultBranch``. Deliberately no fallback to
    "main" or "master": guessing here would mean guessing which branch is protected, and a
    wrong guess is a protected-branch mutation. ``None`` is an honest answer and callers
    treat it as a refusal to proceed.
    """
    if configured is not None:
        return configured

    completed = runner.run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if completed.returncode == 0:
        reference = completed.stdout.decode("utf-8", "replace").strip()
        prefix = "refs/remotes/origin/"
        if reference.startswith(prefix):
            return reference[len(prefix) :]

    completed = runner.run("config", "--get", "init.defaultBranch", check=False)
    if completed.returncode == 0:
        candidate = completed.stdout.decode("utf-8", "replace").strip()
        if candidate:
            return candidate

    return None


def inspect_repository(
    path: Path, *, configured_default_branch: str | None = None, timeout: int = GIT_TIMEOUT_SECONDS
) -> RepositoryInspection:
    """Describe the repository at ``path``. Never mutates, never reaches the network."""
    runner = GitRunner(path, timeout=timeout)

    inside = runner.run("rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        raise NotAGitRepositoryError(
            "path is not inside a Git working tree",
            path=str(path),
            stderr=inside.stderr.decode("utf-8", "replace")[:_MAX_STDERR_CHARS],
        )
    if inside.stdout.decode().strip() != "true":
        raise UnsupportedRepositoryError(
            "path is inside a Git directory but not a working tree", path=str(path)
        )

    if runner.text("rev-parse", "--is-bare-repository") == "true":
        raise UnsupportedRepositoryError(
            "bare repositories are not supported: Claude Away needs a working tree to "
            "inspect and, later, to build in",
            path=str(path),
        )

    root = Path(runner.text("rev-parse", "--show-toplevel")).resolve()
    git_dir = Path(runner.text("rev-parse", "--absolute-git-dir"))

    head = runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    head_commit = head.stdout.decode().strip() if head.returncode == 0 else None

    symbolic = runner.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = (
        symbolic.stdout.decode("utf-8", "surrogateescape").strip()
        if symbolic.returncode == 0
        else None
    )
    # An unborn HEAD is symbolic but has no commit; that is not detachment.
    is_detached = branch is None and head_commit is not None

    status_output = runner.run(
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ).stdout

    remotes = tuple(
        line for line in runner.text("remote", check=False).splitlines() if line.strip()
    )

    return RepositoryInspection(
        root=root,
        git_dir=git_dir,
        head_commit=head_commit,
        branch=branch,
        is_detached=is_detached,
        status=_parse_porcelain_v2(status_output),
        operations_in_progress=_operations_in_progress(git_dir),
        default_branch=_resolve_default_branch(runner, configured_default_branch),
        remotes=remotes,
    )


def resolve_local_ref(path: Path, ref: str, *, timeout: int = GIT_TIMEOUT_SECONDS) -> str | None:
    """Resolve ``ref`` to a commit using only local refs. Returns ``None`` if absent.

    No fetch, ever. If the ref is not present locally, that is a fact the caller must act
    on, not something to repair behind their back.
    """
    if not is_safe_ref(ref):
        raise GitOutputError("refusing to resolve an unsafe ref name", ref=ref)

    runner = GitRunner(path, timeout=timeout)
    # `--` ends option parsing; `^{commit}` forces a commit, so an annotated tag resolves
    # to the commit rather than the tag object.
    completed = runner.run(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}^{{commit}}", "--", check=False
    )
    if completed.returncode == 0:
        return completed.stdout.decode().strip()
    return None
