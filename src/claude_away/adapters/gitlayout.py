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
import re
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
    "redact_config_key",
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


#: URL userinfo inside a configuration key. Git subsection names are arbitrary strings and
#: routinely *are* URLs -- ``url.https://x-access-token:<token>@github.com/.insteadOf`` is
#: what ``actions/checkout`` writes, and ``http.https://user:pass@host/.extraHeader`` and
#: ``lfs.https://user:pass@host/info/lfs.access`` are the same shape. None of those keys is
#: on the allow-list, so a default-deny audit is *guaranteed* to name them in its refusal.
_USERINFO = re.compile(r"(?<=//)[^/@]*@")


def redact_config_key(key: str) -> str:
    """Remove credentials from a configuration key before it reaches a diagnostic.

    Refusing to print configuration *values* was the obvious half and it was done first. It
    is not sufficient: a Git key's middle component is an arbitrary subsection name, and the
    single most common way a credential ends up in a repository's local configuration puts
    it there rather than in a value. Redacting values only would have applied the guarantee
    to exactly the half that did not need it.
    """
    return _USERINFO.sub("<redacted>@", key)


_GITDIR_PREFIX = b"gitdir: "


def _read_bounded(path: Path) -> bytes:
    """Read at most :data:`_MAX_POINTER_BYTES`, without reading the rest first.

    ``read_bytes()[:_MAX_POINTER_BYTES]` was a cap in appearance only: the whole file
    reached memory before the slice, so a 2 GiB sparse ``.git`` -- free to create -- cost
    2 GiB of resident memory and half a minute, once per repository in the walk. The size
    is chosen by the repository, so the memory was too.
    """
    with path.open("rb") as handle:
        return handle.read(_MAX_POINTER_BYTES)


def _reject_nul(raw: bytes, *, path: Path, what: str) -> None:
    """A NUL byte cannot appear in a path, and ``Path.resolve`` raises ``ValueError``.

    Not a ``ClaudeAwayError``, so it escaped every handler: ``enrol_projects`` catches
    ``GitError`` and the CLI catches ``ClaudeAwayError``, so one repository with a NUL in
    its ``.git`` pointer replaced the whole ``awayctl repos`` report with a traceback and
    took every other repository's verdict with it.
    """
    if b"\x00" in raw:
        raise UnsupportedRepositoryError(
            f"the {what} contains a NUL byte, which cannot be part of a path", path=str(path)
        )


def _trim_eol(raw: bytes) -> bytes:
    """Remove trailing ``\\n`` and ``\\r``, and nothing else.

    Git's ``read_gitfile_gently`` and its ``commondir`` reader both trim exactly these two
    characters. ``.strip()`` was used here first and it was a real defect, not a nicety: a
    git directory legitimately named ``store `` -- which ``git init --separate-git-dir`` and
    ``git submodule add`` both produce for a path ending in a space -- came back as
    ``store``, so this module named a git directory Git would never use. With a decoy
    directory at the trimmed name, every bound invocation was then pointed at a *different
    repository*, and the whole inspection agreed with itself while disagreeing with Git.
    Demonstrated before this was written: HEAD and branch reported from the decoy.

    The same mistake had already been made and fixed once in ``GitRunner.path``. Reading
    bytes and trimming exactly what Git trims is the only version of this that is right.
    """
    while raw.endswith((b"\n", b"\r")):
        raw = raw[:-1]
    return raw


def _read_pointer(dot_git: Path) -> Path:
    """Resolve a ``gitdir:`` pointer file -- what submodules and linked worktrees use.

    Deliberately as strict as Git: the file must begin with exactly ``gitdir: ``, one space,
    no leading whitespace. Git rejects anything else with ``invalid gitfile format``, and a
    parser more permissive than Git's would inspect trees Git does not consider repositories
    at all -- and, in the padded-whitespace case, would pick a different path than Git.
    """
    try:
        raw = _read_bounded(dot_git)
    except OSError as exc:
        raise NotAGitRepositoryError(
            "the .git entry could not be read", path=str(dot_git), detail=str(exc)
        ) from exc

    raw = _trim_eol(raw)
    if not raw.startswith(_GITDIR_PREFIX):
        raise UnsupportedRepositoryError(
            "the .git entry is a file but is not a gitdir pointer", path=str(dot_git)
        )
    _reject_nul(raw, path=dot_git, what="gitdir pointer")
    target = os.fsdecode(raw[len(_GITDIR_PREFIX) :])
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
            raw = _read_bounded(commondir_file)
        except OSError as exc:
            raise UnsupportedRepositoryError(
                "commondir could not be read", path=str(worktree), detail=str(exc)
            ) from exc
        # Same rule as the gitdir pointer, for the same reason: Git preserves a trailing
        # space in `commondir`, and `common_dir` is what `local_config` is derived from --
        # so trimming it would move the file the default-deny audit reads.
        _reject_nul(raw, path=commondir_file, what="commondir file")
        target = Path(os.fsdecode(_trim_eol(raw)))
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

#: Exact keys (lower-cased) a normal repository needs. Everything not here is refused, which
#: is what makes the next key Git invents safe by default.
#:
#: The admission rule, applied to every entry below: the key must be unable to change (a)
#: what any command this adapter runs *executes*, or (b) what ``git status`` and
#: ``git ls-files`` *report*. That second clause is why a few plausible-looking keys are
#: absent on purpose. ``core.excludesFile`` and ``core.attributesFile`` name files that
#: decide which untracked paths are hidden and which filter applies, ``status.showUntracked-
#: Files`` decides what status mentions, and ``core.fsmonitor`` decides what Git thinks
#: changed -- all four are masking vectors, so all four stay refused.
#:
#: The list is not minimal, and that is deliberate: an allow-list that refuses a large share
#: of real projects is not a safeguard anybody can use. The first version refused this
#: project's own repository, over ``gc.auto``.
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
        "index.sparse",
        "core.untrackedcache",
        "core.longpaths",
        "core.fscache",
        "core.hidedotfiles",
        "extensions.worktreeconfig",
        "extensions.objectformat",
        "extensions.partialclone",
        "user.name",
        "user.email",
        "user.signingkey",
        "commit.gpgsign",
        "tag.gpgsign",
        "gpg.format",
        "push.default",
        "pull.rebase",
        "submodule.active",
        "lfs.repositoryformatversion",
        "lfs.url",
        # Housekeeping. `gc.auto = 0` alone made the first version of this list refuse the
        # Claude Away repository itself, which is how a safeguard becomes something people
        # switch off.
        "gc.auto",
        "gc.autodetach",
        "gc.pruneexpire",
        "gc.reflogexpire",
        "gc.reflogexpireunreachable",
        "gc.writecommitgraph",
        "maintenance.auto",
        "maintenance.strategy",
        # Per-repository preferences. Inert for a read-only inspection: none of them changes
        # what status reports or what any command executes.
        "pull.ff",
        "pull.twohead",
        "push.autosetupremote",
        "push.followtags",
        "push.gpgsign",
        "fetch.prune",
        "fetch.prunetags",
        "fetch.parallel",
        "fetch.writecommitgraph",
        "rebase.autostash",
        "rebase.autosquash",
        "rebase.updaterefs",
        "rerere.enabled",
        "rerere.autoupdate",
        "diff.algorithm",
        "diff.renames",
        "diff.renamelimit",
        "diff.colormoved",
        "merge.ff",
        "merge.conflictstyle",
        "merge.renamelimit",
        "branch.autosetupmerge",
        "branch.autosetuprebase",
        "branch.sort",
        "tag.sort",
        "log.date",
        "log.follow",
        "color.ui",
        "commit.verbose",
        "help.autocorrect",
        "remote.pushdefault",
        # `init.defaultBranch` is deliberately NOT here. It says what `git init` should name
        # a *new* repository's first branch -- a global preference with no bearing on a
        # repository that already exists -- and setting it locally is how a repository once
        # moved protection off `main`. `_resolve_default_branch` no longer reads it, so the
        # refusal is belt and braces, but a key whose only local use was an attack does not
        # get waved through for tidiness.
        # Monorepo and large-checkout tuning, all of it about how Git stores and reads its
        # own data rather than about what it finds.
        "feature.manyfiles",
        "feature.experimental",
        "index.version",
        "index.threads",
        "core.commitgraph",
        "core.multipackindex",
        "core.splitindex",
        "core.preloadindex",
        "core.bigfilethreshold",
        "core.deltabasecachelimit",
        "checkout.workers",
        "submodule.fetchjobs",
        "pack.threads",
        "http.postbuffer",
        "transfer.fsckobjects",
        # git-flow writes exactly these and nothing else; `gitflow` is not a Git namespace,
        # so listing the keys costs nothing and avoids a prefix allowance.
        "gitflow.branch.master",
        "gitflow.branch.develop",
        "gitflow.prefix.feature",
        "gitflow.prefix.bugfix",
        "gitflow.prefix.release",
        "gitflow.prefix.hotfix",
        "gitflow.prefix.support",
        "gitflow.prefix.versiontag",
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
    ("remote.", ".mirror"),
    ("remote.", ".skipfetchall"),
    ("remote.", ".skipdefaultupdate"),
    # Written by `gh repo fork` / `gh pr`; inert metadata naming the upstream.
    ("remote.", ".gh-resolved"),
    ("branch.", ".remote"),
    ("branch.", ".pushremote"),
    ("branch.", ".merge"),
    ("branch.", ".rebase"),
    ("branch.", ".description"),
    # Written by VS Code, one per branch it has looked at.
    ("branch.", ".vscode-merge-base"),
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
        if (
            key == "core.bare"
            and value.strip().lower() in _BOOL_TRUE
            # `git clone --bare` + `git worktree add` is an ordinary workflow, and the bare
            # repository's `core.bare = true` is also the *common* config of every linked
            # worktree it hosts. Refusing on the value alone rejected a tree with a working
            # `git status`, and the operator could not fix it without breaking Git. A linked
            # worktree is exactly `git_dir != common_dir`, and by construction is not bare.
            and layout.git_dir == layout.common_dir
        ):
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
        if key == "core.worktree" and layout.git_dir == layout.common_dir:
            # Only for the repository that owns the config. For a linked worktree the
            # *common* config is the primary checkout's, and a submodule's common config
            # legitimately carries `core.worktree` pointing at the submodule's own primary
            # checkout -- so comparing it against the linked worktree refused every
            # `git worktree add` made inside a submodule, with no remedy the operator could
            # apply. `git_dir != common_dir` is exactly "this is a linked worktree", and the
            # binding means the value cannot take effect there either way.
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

    if not (includes or offenders):
        return

    # Both are reported in one pass. Raising on includes first meant an operator with an
    # include *and* an unrecognised key fixed one, re-ran, and only then learned about the
    # other -- an avoidable second round trip through a failing unattended run.
    reasons: list[str] = []
    if includes:
        reasons.append(
            "include directives are not followed, because they add configuration this audit "
            "would never see"
        )
    if offenders:
        reasons.append(
            "local configuration is default-deny, so only a small allow-list of keys a "
            "normal repository needs is accepted"
        )
    raise UnsafeRepositoryConfigError(
        "repository-local Git configuration contains keys Claude Away does not support ("
        + "; ".join(reasons)
        + "). Remove them from the file named below, or move the setting to your global "
        "configuration if you trust it",
        path=str(layout.worktree),
        keys=sorted({redact_config_key(key) for key in includes + offenders}),
        config=str(layout.local_config),
        worktree_config=(
            str(layout.worktree_config)
            if extensions_worktree_config and layout.worktree_config is not None
            else None
        ),
        policy_version=ALLOWED_LOCAL_CONFIG_VERSION,
    )
