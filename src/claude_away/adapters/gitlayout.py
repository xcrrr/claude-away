"""Filesystem-first Git layout discovery and default-deny configuration validation.

Four adversarial review rounds each found a critical defect in the previous round's fix, and
every one had the same shape: a repository-controlled configuration key changed what the
controller executed or believed. The last two were the *same* key, ``core.worktree``,
exploited two different ways -- first to switch the config audit off, then to redirect which
directory got inspected while the audit ran happily against the real one.

The lesson is not that a key was missed. It is that the defence was a deny-list, and a
deny-list over Git's configuration space cannot be completed by inspection: Git has hundreds
of keys and adds more every release, and ``core.worktree`` is not even command-bearing --
``git submodule add`` writes it legitimately. Three controls replace it, and all three are
needed:

**Layout comes from the filesystem, not from Git.** ``git rev-parse`` is exactly where
``core.worktree`` gets a vote, so asking Git where the repository is means asking the
repository to describe itself. :func:`discover_layout` reads ``.git`` directly -- directory,
``gitdir:`` pointer file, ``commondir`` -- and treats the *enrolled path* as the worktree,
because that is the one thing the operator actually said.

**Configuration is default-deny.** A small allow-list of keys real repositories need, with
values validated where they affect identity. Anything else is refused, so the next key Git
invents is handled before anyone hears about it. Includes are not followed: an
``include.path`` is a way to add configuration the audit never sees, so the directive itself
is refused.

**Every invocation is bound.** ``--git-dir`` and ``--work-tree`` are passed explicitly from
the validated layout, so no configuration value, working directory, or parent repository can
choose what is inspected. The pinned ``-c`` overrides stay, but as defence in depth rather
than as the trust model.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_away.errors import (
    GitCommandError,
    NotAGitRepositoryError,
    UnsafeRepositoryConfigError,
    UnsupportedRepositoryError,
)

__all__ = [
    "ALLOWED_LOCAL_CONFIG_VERSION",
    "RepositoryLayout",
    "audit_local_config",
    "discover_layout",
]

#: Bump when the allow-list changes, so a refusal can be traced to a policy version.
ALLOWED_LOCAL_CONFIG_VERSION = 1

_MAX_POINTER_BYTES = 4096
_MAX_CONFIG_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class RepositoryLayout:
    """Where a repository's parts actually are, established without asking Git."""

    worktree: Path
    """The enrolled path. Never derived from configuration -- this is the operator's word."""

    git_dir: Path
    common_dir: Path
    local_config: Path
    worktree_config: Path | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "worktree": str(self.worktree),
            "git_dir": str(self.git_dir),
            "common_dir": str(self.common_dir),
            "local_config": str(self.local_config),
            "worktree_config": str(self.worktree_config) if self.worktree_config else None,
        }


def _read_pointer(dot_git: Path) -> Path:
    """Resolve a ``gitdir:`` pointer file -- what submodules and linked worktrees use."""
    try:
        raw = dot_git.read_bytes()[:_MAX_POINTER_BYTES]
    except OSError as exc:
        raise NotAGitRepositoryError(
            "the .git entry could not be read", path=str(dot_git), detail=str(exc)
        ) from exc

    text = os.fsdecode(raw).strip()
    if not text.startswith("gitdir:"):
        raise UnsupportedRepositoryError(
            "the .git entry is a file but is not a gitdir pointer", path=str(dot_git)
        )
    target = text[len("gitdir:") :].strip()
    if not target:
        raise UnsupportedRepositoryError("the gitdir pointer is empty", path=str(dot_git))

    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = dot_git.parent / candidate
    return candidate.resolve()


def _looks_like_a_git_directory(path: Path) -> bool:
    """Whether ``path`` is itself a git directory (the ``git init --bare`` shape)."""
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def discover_layout(path: Path) -> RepositoryLayout:
    """Establish a repository's layout from the filesystem alone.

    No Git process is started, so nothing here can be influenced by the repository's own
    configuration -- which is the entire point. Ambiguity is refused rather than guessed.
    """
    worktree = path.expanduser().resolve()
    if not worktree.exists():
        raise NotAGitRepositoryError("path does not exist", path=str(worktree))
    if not worktree.is_dir():
        raise NotAGitRepositoryError("path is not a directory", path=str(worktree))

    dot_git = worktree / ".git"
    if dot_git.is_dir():
        git_dir = dot_git.resolve()
    elif dot_git.is_file():
        git_dir = _read_pointer(dot_git)
    elif _looks_like_a_git_directory(worktree):
        # The path *is* a git directory rather than containing one -- `git init --bare`.
        # Detected here, from the filesystem, so the diagnosis does not depend on asking a
        # repository to describe itself.
        raise UnsupportedRepositoryError(
            "bare repositories are not supported: Claude Away needs a working tree to "
            "inspect and, later, to build in",
            path=str(worktree),
        )
    else:
        raise NotAGitRepositoryError(
            "no .git directory or gitdir pointer at this path; enrol the repository root",
            path=str(worktree),
        )

    if not git_dir.is_dir():
        raise UnsupportedRepositoryError(
            "the gitdir pointer does not lead to a directory",
            path=str(worktree),
            git_dir=str(git_dir),
        )

    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            raw = commondir_file.read_bytes()[:_MAX_POINTER_BYTES]
        except OSError as exc:
            raise UnsupportedRepositoryError(
                "commondir could not be read", path=str(worktree), detail=str(exc)
            ) from exc
        target = Path(os.fsdecode(raw).strip())
        common_dir = (target if target.is_absolute() else git_dir / target).resolve()
        if not common_dir.is_dir():
            raise UnsupportedRepositoryError(
                "commondir does not lead to a directory",
                path=str(worktree),
                common_dir=str(common_dir),
            )

    # A git directory always has HEAD, and the *common* directory owns objects and refs.
    if not (git_dir / "HEAD").is_file():
        raise UnsupportedRepositoryError(
            "the git directory has no HEAD", path=str(worktree), git_dir=str(git_dir)
        )
    for required in ("objects", "refs"):
        if not (common_dir / required).is_dir():
            raise UnsupportedRepositoryError(
                f"the git directory has no {required}/",
                path=str(worktree),
                common_dir=str(common_dir),
            )

    worktree_config = git_dir / "config.worktree"
    return RepositoryLayout(
        worktree=worktree,
        git_dir=git_dir,
        common_dir=common_dir,
        local_config=common_dir / "config",
        worktree_config=worktree_config if worktree_config.is_file() else None,
    )


# ======================================================================================
# Default-deny configuration allow-list
# ======================================================================================

#: Exact keys (lower-cased) a normal repository needs. Deliberately small: everything not
#: here is refused, which is what makes the next key Git invents safe by default.
_ALLOWED_EXACT = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
        "core.autocrlf",
        "core.eol",
        "core.worktree",
        "core.sparsecheckout",
        "core.sparsecheckoutcone",
        "core.untrackedcache",
        "core.longpaths",
        "core.fscache",
        "core.hidedotfiles",
        "extensions.worktreeconfig",
        "extensions.objectformat",
        "extensions.partialclone",
        "user.name",
        "user.email",
        "commit.gpgsign",
        "push.default",
        "pull.rebase",
        "submodule.active",
        "lfs.repositoryformatversion",
        # The one entry here that is not inert on its own: it names a directory of
        # executables. It is allowed because `core.hooksPath` is pinned to /dev/null on
        # every invocation (see `_PINNED_CONFIG`), so a repository's value cannot apply --
        # and because husky and similar tools write it locally in a large share of real
        # projects, where refusing it would be a safeguard nobody could use. If that pin
        # were ever removed, this line must go with it.
        "core.hookspath",
    }
)

#: Patterned keys, allowed only at an exact proven suffix -- never a whole section. The
#: middle component is the user-chosen name, which is why these cannot be exact matches.
_ALLOWED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("remote.", ".url"),
    ("remote.", ".fetch"),
    ("remote.", ".push"),
    ("remote.", ".pushurl"),
    ("remote.", ".tagopt"),
    ("remote.", ".prune"),
    # Written by `git clone --filter=...`; without these a blobless or treeless clone --
    # increasingly the default in CI -- would be refused.
    ("remote.", ".promisor"),
    ("remote.", ".partialclonefilter"),
    ("branch.", ".remote"),
    ("branch.", ".pushremote"),
    ("branch.", ".merge"),
    ("branch.", ".rebase"),
    ("branch.", ".description"),
    ("submodule.", ".url"),
    ("submodule.", ".active"),
    # Honoured by nobody here: submodules are walked manually rather than through Git's
    # descent, so `ignore` cannot suppress anything. Allowed because `git submodule` and
    # ordinary tooling write it, and refusing it would refuse normal repositories.
    ("submodule.", ".ignore"),
    ("submodule.", ".branch"),
)

#: Refused outright with a specific message, because "unknown key" understates them.
_INCLUDE_PREFIXES = ("include.", "includeif.")

_BOOL_TRUE = {"true", "yes", "on", "1"}
_BOOL_FALSE = {"false", "no", "off", "0", ""}


def _split_records(payload: bytes) -> list[tuple[str, str]]:
    """Parse ``git config --list -z`` into (key, value) pairs.

    ``-z`` emits ``<key>\\n<value>\\0`` per entry. A value may contain newlines, so only the
    first is a separator; a valueless key has no newline at all.
    """
    records: list[tuple[str, str]] = []
    for field in payload.split(b"\x00"):
        if not field:
            continue
        text = os.fsdecode(field)
        key, separator, value = text.partition("\n")
        records.append((key, value if separator else ""))
    return records


def _read_config_file(path: Path, timeout: int) -> list[tuple[str, str]]:
    """Read one configuration file with Git's own parser, inertly.

    ``--file`` performs no repository discovery and loads no effective configuration, so
    this cannot be steered by the repository it is reading. ``--no-includes`` is stated
    explicitly even though ``--file`` does not expand includes on its own -- verified
    directly rather than assumed. It is intent and insurance against that default changing,
    not a load-bearing flag; the refusal of ``include.*`` keys below is what actually stops
    configuration arriving from a file this audit never saw.
    """
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise UnsupportedRepositoryError(
                "configuration file is implausibly large; refusing to parse it",
                config=str(path),
            )
    except OSError as exc:
        raise UnsupportedRepositoryError(
            "configuration file could not be read", config=str(path), detail=str(exc)
        ) from exc

    argv = ["git", "config", "--file", str(path), "--no-includes", "--list", "-z"]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
    }
    environment.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"})

    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=timeout, env=environment, check=False
        )
    except FileNotFoundError as exc:  # pragma: no cover - git absent is covered elsewhere
        raise GitCommandError(argv, 127, "git executable not found") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - bounded by caller
        raise GitCommandError(argv, 124, f"git timed out after {timeout}s") from exc

    if completed.returncode == 1 and not completed.stdout:
        return []  # nothing to list
    if completed.returncode != 0:
        raise GitCommandError(
            argv,
            completed.returncode,
            completed.stderr.decode("utf-8", "replace")[:2000],
        )
    return _split_records(completed.stdout)


def _is_allowed(key: str) -> bool:
    if key in _ALLOWED_EXACT:
        return True
    return any(
        key.startswith(prefix) and key.endswith(suffix) and len(key) > len(prefix) + len(suffix)
        for prefix, suffix in _ALLOWED_PATTERNS
    )


def audit_local_config(layout: RepositoryLayout, *, timeout: int = 60) -> None:
    """Refuse the repository unless every repository-local key is on the allow-list.

    Values are never included in the refusal. A configuration file can hold credentials --
    ``remote.<name>.url`` routinely does -- and a security refusal that prints the thing it
    refused is a new problem rather than a fix.
    """
    offenders: list[str] = []
    includes: list[str] = []

    records = list(_read_config_file(layout.local_config, timeout))

    extensions_worktree_config = any(
        key.lower() == "extensions.worktreeconfig" and value.strip().lower() in _BOOL_TRUE
        for key, value in records
    )
    if extensions_worktree_config and layout.worktree_config is not None:
        records += _read_config_file(layout.worktree_config, timeout)

    for raw_key, value in records:
        key = raw_key.lower()

        if key.startswith(_INCLUDE_PREFIXES):
            includes.append(raw_key)
            continue

        if not _is_allowed(key):
            offenders.append(raw_key)
            continue

        # Values that decide identity or supported operation are validated, not just allowed.
        if key == "core.bare" and value.strip().lower() in _BOOL_TRUE:
            raise UnsupportedRepositoryError(
                "bare repositories are not supported: Claude Away needs a working tree",
                path=str(layout.worktree),
            )
        if key == "core.repositoryformatversion" and value.strip() not in {"0", "1"}:
            raise UnsupportedRepositoryError(
                "unsupported repository format version",
                path=str(layout.worktree),
                version=value.strip(),
            )
        if key == "core.worktree":
            declared = Path(value)
            if not declared.is_absolute():
                declared = layout.git_dir / declared
            try:
                resolved = declared.resolve()
            except OSError:  # pragma: no cover - racing filesystem
                resolved = declared
            if resolved != layout.worktree:
                # Legitimate in a submodule, where Git writes it pointing back at the
                # gitlinked path. A value pointing anywhere else is the round-three and
                # round-four critical, so it is refused rather than merely ignored -- even
                # though the bound runner already means it cannot take effect.
                raise UnsafeRepositoryConfigError(
                    "core.worktree points somewhere other than the enrolled working tree; "
                    "refusing rather than inspecting a directory the repository chose",
                    path=str(layout.worktree),
                    expected=str(layout.worktree),
                    policy_version=ALLOWED_LOCAL_CONFIG_VERSION,
                )

    if includes:
        raise UnsafeRepositoryConfigError(
            "repository-local configuration uses an include directive; includes are not "
            "followed, because they add configuration this audit would never see",
            path=str(layout.worktree),
            keys=sorted(includes),
            policy_version=ALLOWED_LOCAL_CONFIG_VERSION,
        )

    if offenders:
        raise UnsafeRepositoryConfigError(
            "repository-local Git configuration contains keys Claude Away does not support. "
            "Local configuration is default-deny: only a small allow-list of keys a normal "
            "repository needs is accepted. Move the setting to your global configuration if "
            "you trust it, or remove it",
            path=str(layout.worktree),
            keys=sorted(set(offenders)),
            policy_version=ALLOWED_LOCAL_CONFIG_VERSION,
        )
