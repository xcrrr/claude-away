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

**A sanitised environment.** ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_CONFIG_PARAMETERS`` and
friends silently redirect Git at a different repository, or inject arbitrary configuration,
regardless of what ``-C`` says. The whole ``GIT_*`` namespace is therefore removed and only
the handful of variables in :data:`_FORCED_ENV` are put back. Deny-by-default, because Git
keeps adding variables and a hand-maintained list of the dangerous ones is a list that is
one release out of date.

**Refuse rather than guess.** Output this build cannot parse raises
:class:`~claude_away.errors.GitOutputError`. A status parser that skips an entry it does
not recognise reports a dirty repository as clean, and "clean" is the answer that gets
somebody else's uncommitted work built on top of.

**A repository's own configuration is not trusted.** This one is easy to miss, and it is
the reason ``read-only`` was in scare quotes until Milestone 2A's review. Several Git
configuration keys hold *commands that Git executes*, and ``git status`` is enough to fire
them: ``core.fsmonitor`` runs on every invocation, and a ``filter.<driver>.clean`` runs
whenever a tracked file's content has to be examined. A repository is data that Claude Away
is pointed at -- and in Milestone 2B it is data Claude itself can write -- so honouring
those keys would mean the deterministic controller executes code chosen by the thing it is
supposed to be supervising. Worse than the execution: an ``fsmonitor`` hook decides *what
Git thinks changed*, so a hostile one makes a modified worktree report clean, which is
precisely the guard :mod:`claude_away.core.base_revision` exists to provide.

Two defences, because neither is sufficient alone. Keys that name a command and have a
fixed name are pinned on every invocation via ``-c`` (highest precedence, so the
repository's value never applies). Keys that cannot be pinned because the name is
user-chosen -- ``filter.*``, ``diff.*``, ``merge.*`` -- are audited before the first command
that could trigger them, and a repository that sets one *in its own config* is refused.
Scope matters there: the operator's global configuration is trusted (that is where
``git lfs install`` puts its filters), the repository's is not.
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
    GitError,
    GitOutputError,
    NotAGitRepositoryError,
    UnsafeRepositoryConfigError,
    UnsupportedGitVersionError,
    UnsupportedRepositoryError,
)

__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "MINIMUM_GIT_VERSION",
    "GitRunner",
    "RepositoryInspection",
    "RepositoryOperation",
    "SubmoduleState",
    "WorktreeStatus",
    "inspect_repository",
    "is_safe_ref",
    "resolve_local_ref",
]

MINIMUM_GIT_VERSION = "2.26"
"""The oldest Git this adapter works with.

Set by the newest flag in use: ``--porcelain=v2`` needs 2.11, ``--absolute-git-dir`` 2.13,
``--no-optional-locks`` 2.15, and ``git config --list --show-scope`` -- which the
repository-configuration audit depends on -- needs 2.26.
"""

GIT_TIMEOUT_SECONDS = 60
"""Every Git call is bounded. An unattended run must not hang forever on a stuck lock."""

_MAX_STDERR_CHARS = 2_000

# Deny-by-default over the whole GIT_* namespace. An enumerated list of dangerous variables
# was the previous design and it leaked: GIT_CONFIG_PARAMETERS was missing, and it alone is
# enough to inject any configuration key -- including one that names a command to run -- into
# every "read-only" call. Removing the namespace wholesale means the next variable Git adds
# is handled before anybody hears about it.
_STRIPPED_ENV_PREFIXES = ("GIT_",)

# Non-GIT_ variables that steer Git into an interactive or network path.
_STRIPPED_ENV = ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")

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

# Configuration keys whose values Git executes as commands, pinned on every invocation.
# `-c` outranks system, global and repository configuration, so whatever the repository (or
# the operator) has set for these does not apply to our calls.
#
# `core.fsmonitor` is the one that matters most and is not optional: besides running a
# command, it decides what Git reports as changed, so a repository could otherwise make
# itself look clean. The rest are pinned because they cost nothing and remove a whole class
# of "does `git <verb>` reach this key?" reasoning that would have to be redone for every
# command added later.
_PINNED_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.pager", "cat"),
    ("core.editor", "false"),
    ("core.sshCommand", "false"),
    ("core.askPass", ""),
    ("core.gitProxy", ""),
    ("core.alternateRefsCommand", ""),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("gpg.program", "false"),
    ("uploadpack.packObjectsHook", ""),
    ("protocol.ext.allow", "never"),
)

# Command-bearing keys whose middle component is user-chosen, so `-c` cannot pin them: there
# is no key to override until you know the driver's name. These are audited instead, and a
# repository that sets one in its *own* configuration is refused.
#
# Matched case-insensitively against `git config --list --show-scope`, which lower-cases
# section and key while preserving the middle component verbatim.
_UNPINNABLE_COMMAND_KEYS: tuple[tuple[str, str], ...] = (
    ("filter.", ".clean"),
    ("filter.", ".smudge"),
    ("filter.", ".process"),
    ("diff.", ".textconv"),
    ("diff.", ".command"),
    ("diff.", ".external"),
    ("merge.", ".driver"),
    ("trailer.", ".command"),
    ("trailer.", ".cmd"),
    # `gpg.<format>.program` and `credential.<url>.helper` take a middle component too, so
    # pinning the bare `gpg.program` / `credential.helper` does not cover them.
    ("gpg.", ".program"),
    ("credential.", ".helper"),
)

# Scopes that the repository itself controls. `git config --list --show-scope` reports
# `system`, `global`, `local`, `worktree` and `command`; only the middle two live inside the
# repository and can therefore be written by a checkout, an unpacked archive, or -- the case
# this project has to assume -- Claude working in the repository.
_UNTRUSTED_CONFIG_SCOPES = frozenset({"local", "worktree"})


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
    unverifiable: tuple[str, ...] = ()
    """Paths whose index bits stop ``git status`` from reporting them at all.

    ``git update-index --assume-unchanged`` and ``--skip-worktree`` are ordinary developer
    habits for a local config file, and they tell Git not to look. ``git status`` then says
    nothing about the path even when its content differs from HEAD -- and the difference
    survives ``git checkout -b``, so it *would* be carried into a new branch. These are not
    known-dirty paths; they are paths whose cleanliness cannot be established, which for
    this purpose is the same answer.
    """

    @property
    def is_clean(self) -> bool:
        """Clean means *nothing* would be carried into a new branch.

        Untracked files count. They are the ones most likely to be accidentally added by a
        broad ``git add``, and a task that starts on top of them cannot say afterwards
        which changes were its own.

        So do paths Git has been told not to check. "We looked and found nothing" and "we
        were told not to look" are different statements, and only the first supports a
        claim that the tree is clean.
        """
        return not (
            self.staged
            or self.unstaged
            or self.untracked
            or self.unmerged
            or self.dirty_submodules
            or self.unverifiable
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
            "unverifiable": list(self.unverifiable),
        }


@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    """Everything M2A can say about a repository without touching it."""

    root: Path
    git_dir: Path
    common_dir: Path
    """The shared object/ref store. Differs from ``git_dir`` only for a linked worktree,
    which is exactly why it is recorded: two linked worktrees are two working trees over
    one ref namespace, and only this value says so."""

    head_commit: str | None
    """``None`` on an unborn branch -- a fresh ``git init`` with no commit yet."""

    branch: str | None
    """``None`` when HEAD is detached."""

    is_detached: bool
    status: WorktreeStatus
    operations_in_progress: tuple[RepositoryOperation, ...]
    default_branch: str | None
    default_branch_source: str | None
    """``"configured"``, ``"origin_head"``, or ``None``. Who said so, not just what."""

    discovered_default_branch: str | None
    """What ``refs/remotes/origin/HEAD`` says, regardless of what the operator declared.

    Recorded even when it is not the effective answer, so a caller can protect both and a
    repository can only ever add to the protected set, never move it.
    """

    remotes: tuple[str, ...]

    @property
    def is_unborn(self) -> bool:
        return self.head_commit is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "common_dir": str(self.common_dir),
            "head_commit": self.head_commit,
            "branch": self.branch,
            "detached": self.is_detached,
            "unborn": self.is_unborn,
            "default_branch": self.default_branch,
            "default_branch_source": self.default_branch_source,
            "discovered_default_branch": self.discovered_default_branch,
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
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _STRIPPED_ENV and not key.startswith(_STRIPPED_ENV_PREFIXES)
        }
        environment.update(_FORCED_ENV)
        return environment

    def run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Invoke Git with the given arguments.

        ``-C`` rather than ``cwd=`` so the target is explicit in the recorded argv, and
        ``--no-optional-locks`` so inspection never contends with a human's shell. The
        pinned ``-c`` overrides come before the subcommand because that is the only position
        Git accepts them in, and they outrank every configuration file.
        """
        for argument in arguments:
            if "\x00" in argument:
                raise GitOutputError("git argument contains a NUL byte", argument=argument)

        pinned: list[str] = []
        for key, value in _PINNED_CONFIG:
            pinned += ["-c", f"{key}={value}"]

        argv = ["git", "-C", str(self.cwd), "--no-optional-locks", *pinned, *arguments]
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

    def path(self, *arguments: str, check: bool = True) -> Path:
        """Read a filesystem path from Git's output, without damaging it.

        :meth:`text` is wrong for paths in two ways that only show up on the unusual
        repository. It strips *all* trailing whitespace, so a directory legitimately named
        ``trail `` came back as ``trail`` -- a path that does not exist, which then produced
        an unfollowable error telling the operator to enrol a root they cannot enrol. And it
        decodes with ``errors="replace"``, so a path containing a non-UTF-8 byte became one
        containing U+FFFD; the git directory then did not exist, and interrupted-operation
        detection -- pure filesystem probing against that path -- silently returned "no
        operation in progress" for a repository sitting in a conflicted merge.

        ``os.fsdecode`` round-trips through ``os.fsencode``, and only the trailing newline
        Git appends is removed.
        """
        raw = self.run(*arguments, check=check).stdout
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        return Path(os.fsdecode(raw))


def _decode(raw: bytes) -> str:
    """Decode a path from Git.

    ``surrogateescape`` so a filename that is not valid UTF-8 round-trips instead of
    raising. Refusing to inspect a repository because one file has an odd byte sequence
    would be a denial of service on a legitimate project.
    """
    return raw.decode("utf-8", "surrogateescape")


def _parse_porcelain_v2(payload: bytes, *, unverifiable: tuple[str, ...] = ()) -> WorktreeStatus:
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
        unverifiable=unverifiable,
    )


def _repository_defined_command_config(runner: GitRunner) -> list[str]:
    """Names of command-bearing configuration keys the *repository* sets for itself.

    ``git config --list`` executes nothing -- it reads files -- so this is safe to run
    before the commands that would fire a hook. ``--show-scope`` is what makes the check
    usable rather than merely strict: ``git lfs install`` writes ``filter.lfs.clean`` to the
    operator's global configuration, and refusing every LFS repository would be a bug, not a
    safeguard. Only ``local`` and ``worktree`` scope is the repository speaking.

    Returns the offending key names so the caller can name them in the refusal. An operator
    told "this repository is unsafe" and not told which key would have to go looking.
    """
    completed = runner.run("config", "--list", "--show-scope", "-z", check=False)
    if completed.returncode == 1 and not completed.stdout:
        # `git config --list` exits 1 when there is genuinely nothing to list. `rev-parse`
        # has already established this is a working tree, so a repository with no config
        # entries at all is simply one with nothing to refuse.
        return []
    if completed.returncode != 0:
        # Anything else means the audit did not run, and an audit that did not run must
        # never read as "found nothing" -- that is the failure mode this whole function
        # exists to prevent, and returning [] here reintroduced it one level up. A Git
        # without `--show-scope` exits 129, and silently accepted every hostile repository.
        stderr = completed.stderr.decode("utf-8", "replace")
        if "unknown option" in stderr or "unknown switch" in stderr:
            raise UnsupportedGitVersionError(
                f"git {MINIMUM_GIT_VERSION} or newer is required: this build cannot check "
                f"whether the repository's configuration names commands Git would execute, "
                f"and will not inspect a repository it cannot check",
                stderr=stderr[:_MAX_STDERR_CHARS],
            )
        raise GitCommandError(
            list(completed.args),
            completed.returncode,
            stderr[:_MAX_STDERR_CHARS],
        )

    # With `-z` the stream is a flat sequence of NUL-terminated fields that alternate
    # `<scope>` and `<key>\n<value>` -- *not* one NUL-separated record per entry. Getting
    # this wrong parses cleanly and finds nothing, which is the failure mode a security
    # check must not have, so `test_the_audit_parses_real_git_output` pins it against the
    # bytes real Git emits rather than against a fixture written from this description.
    fields = completed.stdout.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()

    offenders: list[str] = []
    for index in range(0, len(fields) - 1, 2):
        scope = fields[index].decode("utf-8", "surrogateescape")
        if scope not in _UNTRUSTED_CONFIG_SCOPES:
            continue
        # A value may contain newlines; the key is everything before the first one.
        key = fields[index + 1].decode("utf-8", "surrogateescape").split("\n", 1)[0]
        folded = key.lower()
        for prefix, suffix in _UNPINNABLE_COMMAND_KEYS:
            if folded.startswith(prefix) and folded.endswith(suffix) and key not in offenders:
                offenders.append(key)
    return offenders


def _gitlink_paths(runner: GitRunner) -> tuple[str, ...]:
    """Repository-relative paths of gitlink entries -- that is, submodules.

    Read from the index (``git ls-files -s``), not from ``.gitmodules`` and not from
    ``git submodule``: the index is what ``git status`` actually consults when it decides
    to descend, and it is a plain file read that executes nothing. A submodule removed from
    ``.gitmodules`` but still gitlinked is still descended into, so ``.gitmodules`` would be
    the wrong source.
    """
    completed = runner.run("ls-files", "-s", "-z", check=False)
    if completed.returncode != 0:
        raise GitCommandError(
            list(completed.args),
            completed.returncode,
            completed.stderr.decode("utf-8", "replace")[:_MAX_STDERR_CHARS],
        )

    paths: list[str] = []
    for record in completed.stdout.split(b"\x00"):
        if not record:
            continue
        # "<mode> <object> <stage>\t<path>"
        metadata, separator, raw_path = record.partition(b"\t")
        if separator and metadata.startswith(b"160000 "):
            paths.append(_decode(raw_path))
    return tuple(paths)


#: How deep to follow nested submodules. Submodules nest legitimately but not deeply, and a
#: bound means a pathological tree cannot turn inspection into an unbounded walk.
_MAX_SUBMODULE_DEPTH = 8


def _assert_configuration_is_inert(runner: GitRunner, path: Path, _depth: int = 0) -> None:
    """Refuse a repository -- or any submodule of it -- that names commands Git would run.

    The recursion is the whole point, and its absence was a critical hole. ``git status
    --ignore-submodules=none`` spawns a child ``git status`` inside every gitlinked
    submodule, and that child reads the *submodule's* own ``.git/config``. Auditing only the
    superproject left a repository free to put ``filter.<driver>.clean`` one directory down
    and have the controller run it -- while the ``-c`` pins, which do propagate into the
    child, made the other half of the defence look like it was working.

    The masking half is worse than the execution half. A clean filter defines what Git
    compares the worktree against, so a driver that always emits the pristine blob makes a
    genuinely modified submodule report clean: ``awayctl repos`` said ``ready=1/1``,
    ``dirty_submodules=[]`` and ``resolved=True`` for a tree carrying somebody else's
    uncommitted work. Demonstrated with a real ``git submodule add`` submodule before this
    was written.
    """
    offenders = list(_repository_defined_command_config(runner))

    if _depth < _MAX_SUBMODULE_DEPTH:
        root = runner.path("rev-parse", "--show-toplevel").resolve()
        for relative in _gitlink_paths(runner):
            worktree = (root / relative).resolve()
            if not worktree.is_dir():
                continue  # gitlinked but not checked out: nothing to descend into

            child = GitRunner(worktree, timeout=runner.timeout)
            try:
                child_root = child.path("rev-parse", "--show-toplevel").resolve()
            except GitError as exc:
                # Git could not say what this path is -- a corrupt submodule config does
                # this. Skipping would be the wrong answer twice over: `git status` descends
                # regardless and fails there with an untyped error, and "we could not look"
                # must never read as "nothing to find".
                raise UnsafeRepositoryConfigError(
                    "a submodule's Git configuration could not be read, and `git status` "
                    "descends into it regardless; Claude Away will not inspect a repository "
                    "it cannot fully check",
                    path=str(path),
                    submodule=relative,
                    detail=exc.message,
                ) from exc
            if child_root != worktree:
                # A gitlinked path that is not itself a repository root. Git walks *up* from
                # `-C`, so without this check the child runner resolves back to the
                # superproject and we audit it again -- once per level, to the depth cap --
                # while learning nothing. `git status` has no separate config to read here
                # either, because there is no repository at this path to read one from.
                continue

            try:
                _assert_configuration_is_inert(child, worktree, _depth + 1)
            except UnsafeRepositoryConfigError as exc:
                # Prefixed with the submodule path so the operator is told *where* to look;
                # "filter.pwn.clean" alone would send them to the wrong .git/config.
                offenders.extend(f"{relative}:{key}" for key in exc.details.get("keys", ()))
            except GitError as exc:
                # A submodule we cannot read is a submodule we cannot clear. `git status`
                # descends into it regardless, so refusing is the only honest answer -- the
                # same rule as the audit itself: a check that did not run is not a pass.
                raise UnsafeRepositoryConfigError(
                    "a submodule's Git configuration could not be checked, and `git status` "
                    "descends into it regardless; Claude Away will not inspect a repository "
                    "it cannot fully check",
                    path=str(path),
                    submodule=relative,
                ) from exc

    if offenders:
        raise UnsafeRepositoryConfigError(
            "repository-local Git configuration defines commands that Git would execute "
            "during inspection; Claude Away will not run them. Move the setting to your "
            "global configuration if you trust it, or remove it",
            path=str(path),
            keys=offenders,
        )


def _unverifiable_paths(runner: GitRunner) -> tuple[str, ...]:
    """Paths flagged ``assume-unchanged`` or ``skip-worktree`` in the index.

    ``git ls-files -v`` tags each entry with a letter; the letter is lower-cased when the
    entry is assume-unchanged, and ``S`` marks skip-worktree. Both make ``git status``
    silent about the path regardless of its content, which is why they have to be read
    separately -- status output cannot report what status does not look at.
    """
    completed = runner.run("ls-files", "-v", "-z", check=False)
    if completed.returncode != 0:
        return ()

    flagged: list[str] = []
    for record in completed.stdout.split(b"\x00"):
        if len(record) < 3 or record[1:2] != b" ":
            continue
        tag = record[0:1]
        if tag.islower() or tag == b"S":
            flagged.append(_decode(record[2:]))
    return tuple(flagged)


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

    # The sequencer outlives CHERRY_PICK_HEAD. `git cherry-pick A B` that conflicts on A and
    # is then finished with a plain `git commit` rather than `--continue` removes the head
    # marker but leaves the remaining picks queued in sequencer/todo -- `git status` still
    # says "Cherry-pick currently in progress", and `--continue` still works. Missing this
    # is the difference between refusing a half-applied series and branching from the middle
    # of one. `sequencer/opts` records which verb queued it.
    if (git_dir / "sequencer" / "todo").exists():
        queued = RepositoryOperation.CHERRY_PICK
        try:
            options = (git_dir / "sequencer" / "opts").read_text(encoding="utf-8", errors="replace")
        except OSError:
            options = ""
        if "revert" in options:
            queued = RepositoryOperation.REVERT
        if queued not in found:
            found.append(queued)

    # `git am` shares rebase-apply with `git rebase`; the applypatch marker disambiguates.
    if (git_dir / "rebase-apply" / "applying").exists():
        if RepositoryOperation.REBASE in found:
            found.remove(RepositoryOperation.REBASE)
        found.append(RepositoryOperation.APPLY_MAILBOX)

    return tuple(found)


def _discover_default_branch(runner: GitRunner) -> str | None:
    """The default branch as the *repository* records it, from ``refs/remotes/origin/HEAD``.

    Computed unconditionally, even when the operator has declared a branch, because the
    caller that decides what to protect needs both answers. ``_protected_branches`` claimed
    to take the union of declared and discovered, and could not: this function used to
    return the configured value and never look, so the set was always one or the other and
    a decoy written into the repository *replaced* the protected branch instead of adding
    to it. A union you cannot compute is not a union.
    """
    completed = runner.run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if completed.returncode != 0:
        return None
    reference = completed.stdout.decode("utf-8", "replace").strip()
    prefix = "refs/remotes/origin/"
    if not reference.startswith(prefix):
        return None
    candidate = reference[len(prefix) :]
    return candidate if is_safe_ref(candidate) else None


def _resolve_default_branch(
    runner: GitRunner, configured: str | None
) -> tuple[str | None, str | None, str | None]:
    """Determine the repository's default branch without touching the network.

    Returns ``(branch, source)``, where ``source`` is ``"configured"``, ``"origin_head"``
    or ``None``. The provenance is not decoration: only one of those sources is the
    *operator* speaking, and the branch this function names is the branch that gets
    protected. A caller handed a bare string cannot tell a declaration from an assertion
    the repository made about itself.

    Order: the operator's explicit configuration, then ``refs/remotes/origin/HEAD`` if a
    previous clone recorded one. Deliberately no fallback to "main" or "master": guessing
    here would mean guessing which branch is protected, and a wrong guess is a
    protected-branch mutation. ``None`` is an honest answer and callers treat it as a
    refusal to proceed.

    ``init.defaultBranch`` used to be the third source and has been removed. It says what
    ``git init`` should name a *new* repository's first branch -- it is a personal
    preference in ``~/.gitconfig``, with no relationship to the default branch of a
    repository that already exists and may have been cloned, renamed, or created under a
    different setting. Consulting it produced a confident ``resolved`` answer, indis-
    tinguishable downstream from a fact, for what was a guess about which branch to protect.
    It was also repository-overridable, so a line appended to ``.git/config`` moved
    protection off ``main``.

    ``refs/remotes/origin/HEAD`` is repository-controlled too, and is kept because it is a
    real record written by a real clone rather than an unrelated preference -- but it is
    labelled ``origin_head`` precisely so that a caller deciding what to protect can weigh
    it differently from an operator's declaration.

    Values that are not usable ref names are discarded rather than returned, since anything
    with write access to the repository can put an option-shaped string there. Returning
    ``None`` turns that into an ``UNKNOWN_DEFAULT_BRANCH`` refusal against the one
    repository concerned; returning the value would raise out of whichever caller resolved
    it next and, in ``awayctl repos``, take every other repository's verdict down with it.
    """
    discovered = _discover_default_branch(runner)
    if configured is not None:
        return configured, "configured", discovered
    if discovered is not None:
        return discovered, "origin_head", discovered
    return None, None, discovered


def inspect_repository(
    path: Path, *, configured_default_branch: str | None = None, timeout: int = GIT_TIMEOUT_SECONDS
) -> RepositoryInspection:
    """Describe the repository at ``path``. Never mutates, never reaches the network."""
    runner = GitRunner(path, timeout=timeout)

    inside = runner.run("rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        stderr = inside.stderr.decode("utf-8", "replace")
        if "unknown option" in stderr or "unknown switch" in stderr:
            # An option this build relies on was rejected, which means the Git on PATH is
            # older than the adapter requires -- not that the path is not a repository.
            # Reporting it as "not a repository" sent the operator to check their config
            # when the problem was their toolchain.
            raise UnsupportedGitVersionError(
                f"the installed git does not accept an option this build requires; "
                f"git {MINIMUM_GIT_VERSION} or newer is needed",
                path=str(path),
                stderr=stderr[:_MAX_STDERR_CHARS],
            )
        raise NotAGitRepositoryError(
            "path is not inside a Git working tree",
            path=str(path),
            stderr=stderr[:_MAX_STDERR_CHARS],
        )
    # Bare is checked *before* "not a working tree", because a bare repository answers
    # `false` to both questions and would otherwise get the vaguer of the two messages.
    # That is what happened until a test stopped matching on `str(exception)` -- which
    # includes the path, and the fixture's path happened to contain the word "bare".
    if runner.text("rev-parse", "--is-bare-repository", check=False) == "true":
        raise UnsupportedRepositoryError(
            "bare repositories are not supported: Claude Away needs a working tree to "
            "inspect and, later, to build in",
            path=str(path),
        )

    if inside.stdout.decode().strip() != "true":
        raise UnsupportedRepositoryError(
            "path is inside a Git directory but not a working tree", path=str(path)
        )

    # Before `status`, which is the first command that could fire a repository-defined
    # filter driver. `rev-parse` above reads no worktree content and triggers nothing.
    _assert_configuration_is_inert(runner, path)

    root = runner.path("rev-parse", "--show-toplevel").resolve()
    git_dir = runner.path("rev-parse", "--absolute-git-dir")
    # `--git-common-dir` may come back relative (".git") depending on the Git version, so
    # it is anchored to the working tree root rather than to the process cwd.
    common_dir = runner.path("rev-parse", "--git-common-dir")
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    common_dir = common_dir.resolve()

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

    default_branch, default_branch_source, discovered_default_branch = _resolve_default_branch(
        runner, configured_default_branch
    )

    return RepositoryInspection(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        head_commit=head_commit,
        branch=branch,
        is_detached=is_detached,
        status=_parse_porcelain_v2(status_output, unverifiable=_unverifiable_paths(runner)),
        operations_in_progress=_operations_in_progress(git_dir),
        default_branch=default_branch,
        default_branch_source=default_branch_source,
        discovered_default_branch=discovered_default_branch,
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
