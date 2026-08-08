"""The deterministic safety policy.

One place decides what Claude Away is permitted to do to a repository. Not a boolean
checked at each call site -- those drift, and the drift is always in the permissive
direction, because the call site that forgot a check is the one that shipped.

Four invariants govern the design:

**Absence of permission is denial.** The operation table is exhaustive and closed. An
operation nobody has classified is denied, so adding a capability without deciding its
policy fails shut rather than open.

**Permission is never inferred.** ``allowPush`` does not imply force-push;
``allowCommit`` does not imply committing to the default branch; ``allowMerge`` does not
imply merging into a protected one. Each is its own question with its own answer.

**A model cannot widen authority.** Nothing here reads model output. Decisions are a pure
function of configuration and the named operation, which is what makes them testable and
what makes "Claude decided it was fine" impossible to express.

**Every decision is explainable.** A :class:`PolicyDecision` carries the rule that produced
it, so a return briefing can say *why* something did not happen instead of leaving a hole
in the narrative.

Milestone 2A performs none of these operations. It builds the gate they will have to pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from claude_away.errors import PolicyDeniedError, ValidationError

__all__ = [
    "Operation",
    "PolicyDecision",
    "PolicyRule",
    "SafetyPolicy",
    "normalise_ref",
    "normalise_repo_path",
]


#: A rule maps (policy, operation, branch, paths) to a decision. Typed rather than `Any`
#: so a handler with the wrong shape is a type error instead of a runtime surprise.
PolicyRule = Callable[["SafetyPolicy", "Operation", "str | None", Sequence[str]], "PolicyDecision"]


class Operation(str, Enum):
    """Every operation the policy knows about.

    Closed by construction: :meth:`SafetyPolicy.evaluate` takes this enum, so an operation
    that does not appear here cannot be requested at all, and one added without a rule is
    caught by ``test_every_operation_has_a_rule`` rather than defaulting to allow.
    """

    # Read-only. The whole of Milestone 2A.
    INSPECT = "inspect"
    READ_FILE = "read_file"

    # Local mutation, gated by configuration.
    CREATE_BRANCH = "create_branch"
    CREATE_WORKTREE = "create_worktree"
    WRITE_FILE = "write_file"
    COMMIT = "commit"

    # Publishing.
    PUSH = "push"
    FORCE_PUSH = "force_push"
    OPEN_PULL_REQUEST = "open_pull_request"
    MERGE = "merge"

    # Everything the project refuses to make routine.
    DEPLOY = "deploy"
    DELETE_BRANCH = "delete_branch"
    DESTRUCTIVE = "destructive"
    REWRITE_HISTORY = "rewrite_history"


#: Operations that alter the repository. Used to decide when path and branch guards apply.
_MUTATING = frozenset(
    {
        Operation.CREATE_BRANCH,
        Operation.CREATE_WORKTREE,
        Operation.WRITE_FILE,
        Operation.COMMIT,
        Operation.PUSH,
        Operation.FORCE_PUSH,
        Operation.OPEN_PULL_REQUEST,
        Operation.MERGE,
        Operation.DEPLOY,
        Operation.DELETE_BRANCH,
        Operation.DESTRUCTIVE,
        Operation.REWRITE_HISTORY,
    }
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The answer, and the rule that produced it."""

    operation: Operation
    allowed: bool
    rule: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "detail": dict(self.detail),
        }

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise PolicyDeniedError(
                operation=self.operation.value,
                rule=self.rule,
                reason=self.reason,
                detail=self.detail,
            )


#: Characters that look like a glob. Protected paths are matched component-wise, not by
#: globbing, so an entry containing these would silently protect nothing.
_GLOB_CHARACTERS = ("*", "?", "[")


def normalise_repo_path(path: str) -> PurePosixPath:
    """Normalise a repository-relative path for protected-path matching.

    Defined once, here, so that two call sites cannot disagree about whether ``./src`` and
    ``src`` are the same thing. Backslashes fold to forward slashes because a Windows-style
    path must not slip past a guard written with POSIX separators.

    Two inputs are refused rather than reinterpreted. ``..`` cannot be normalised away: a
    protected-path check is meaningless if the path can escape the repository. And an
    **absolute** path is not repository-relative, so there is no honest answer to give --
    stripping the leading slash used to turn ``/home/me/repo/infra/x`` into
    ``home/me/repo/infra/x``, which shares no component with the entry ``infra`` and
    therefore reported an obviously protected file as unprotected. Callers holding an
    absolute path (a tool-use hook, for one, since Claude Code reports absolute paths) must
    relativise it against the repository root first; :meth:`SafetyPolicy.is_path_protected`
    treats anything it cannot normalise as protected, so the failure direction is closed.
    """
    cleaned = path.replace("\\", "/").strip()
    if cleaned.startswith("/"):
        raise ValueError(
            f"path must be repository-relative, not absolute: {path!r}; relativise it "
            "against the repository root before asking whether it is protected"
        )
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    candidate = PurePosixPath(cleaned)
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"repository-relative path must not contain '..': {path!r}")
    return candidate


def _validate_protected_paths(entries: Sequence[str]) -> tuple[str, ...]:
    """Check every configured protected path, refusing the ones that cannot be honoured.

    Previously an entry that failed to normalise was skipped, which is the wrong direction
    for a guard: ``../secrets`` protected nothing, raised nothing, and ``awayctl policy``
    still printed it under "protected paths" -- positive confirmation of protection that did
    not exist. Glob entries did the same thing by a different route, since matching is
    component-wise and ``infra/**`` is a literal path component that no real path equals.

    An entry we cannot honour is a configuration error, and configuration errors are the
    operator's to fix before the run starts, not ours to quietly drop.
    """
    validated: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ValidationError(
                "safety.protectedPaths entries must be strings", entry=repr(entry)
            )
        if any(character in entry for character in _GLOB_CHARACTERS):
            raise ValidationError(
                "safety.protectedPaths does not support glob patterns; entries match a path "
                "and everything under it, so name the directory itself",
                entry=entry,
            )
        try:
            normalise_repo_path(entry)
        except ValueError as exc:
            raise ValidationError(
                f"safety.protectedPaths entry cannot be honoured: {exc}", entry=entry
            ) from exc
        validated.append(entry)
    return tuple(validated)


def normalise_ref(ref: str) -> str:
    """Reduce a branch to the short name protected-branch entries are written with.

    Git hands back different spellings depending on which incantation produced the value:
    ``git symbolic-ref HEAD`` gives ``refs/heads/main`` while ``--short`` gives ``main``.
    Comparing raw strings meant ``refs/heads/main`` was not protected by an entry of
    ``main`` -- a silent allow that depends on nothing but which command a future caller
    happened to use. Remote-tracking spellings collapse to the same short name too: that
    over-protects rather than under-protects, which is the direction to err in.
    """
    cleaned = ref.strip()
    for prefix in ("refs/heads/", "heads/", "refs/remotes/", "remotes/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            # A remote-tracking name still carries the remote: origin/main -> main.
            if prefix in ("refs/remotes/", "remotes/") and "/" in cleaned:
                cleaned = cleaned.split("/", 1)[1]
            break
    return cleaned


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """A pure, testable view of ``config.safety``."""

    # Every flag defaults to False, including this one. It defaulted to True until review
    # pointed out that the single deny-by-default module had exactly one field that
    # defaulted to allow -- and that `SafetyPolicy(protected_paths=..., ...)` is the natural
    # call shape for seeding guards, so the grant would have been handed out by the code
    # most likely to be written next.
    allow_commit: bool = False
    allow_push: bool = False
    allow_merge: bool = False
    allow_deploy: bool = False
    allow_destructive: bool = False
    protected_paths: tuple[str, ...] = ()
    protected_branches: tuple[str, ...] = ()
    """Branches that must not be mutated. The default branch is added by the caller."""

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], *, protected_branches: Sequence[str] = ()
    ) -> SafetyPolicy:
        """Build a policy from a configuration document.

        Values are type-checked rather than coerced. ``bool("false")`` is ``True``, so a
        configuration saying ``"allowPush": "false"`` used to enable pushing -- the single
        worst way to misread a safety flag. Anything that is not a real boolean is a
        configuration error here, not a silent grant.
        """
        raw = config.get("safety") or {}
        if not isinstance(raw, Mapping):
            raise ValidationError("safety must be an object", safety=repr(raw))

        def flag(key: str) -> bool:
            value = raw.get(key, False)
            if not isinstance(value, bool):
                raise ValidationError(
                    f"safety.{key} must be true or false, not a string or number",
                    key=key,
                    value=repr(value),
                )
            return value

        paths = raw.get("protectedPaths") or ()
        if isinstance(paths, str) or not isinstance(paths, Sequence):
            # `tuple("infra")` is ('i','n','f','r','a'), which protects nothing at all.
            raise ValidationError(
                "safety.protectedPaths must be a list of strings", protected_paths=repr(paths)
            )

        return cls(
            allow_commit=flag("allowCommit"),
            allow_push=flag("allowPush"),
            allow_merge=flag("allowMerge"),
            allow_deploy=flag("allowDeploy"),
            allow_destructive=flag("allowDestructive"),
            protected_paths=_validate_protected_paths(list(paths)),
            protected_branches=tuple(branch for branch in protected_branches if branch),
        )

    # ---------------------------------------------------------------- protected paths

    def is_path_protected(self, path: str) -> bool:
        """Whether ``path`` is covered by a protected-path entry.

        Semantics, fixed here rather than re-invented per call site: an entry matches a
        path when it is equal to it or is a **path-component prefix** of it. So ``infra``
        protects ``infra`` and ``infra/deploy.tf`` but not ``infrastructure.md`` -- prefix
        matching on raw strings would catch the latter and surprise everyone.

        Matching is case-sensitive, like Git's own index. On a case-insensitive filesystem
        that means ``INFRA/x`` is not caught by ``infra``; that is a known limitation
        recorded in the implementation plan rather than papered over with a fold that would
        be wrong on Linux.
        """
        try:
            candidate = normalise_repo_path(path)
        except ValueError:
            return True  # a path we cannot normalise is treated as protected: fail closed

        for entry in self.protected_paths:
            try:
                protected = normalise_repo_path(entry)
            except ValueError:  # pragma: no cover - _validate_protected_paths rejects these
                # Reached only if a policy was constructed directly with an entry that
                # from_config would have refused. Treat it as protected: an entry we cannot
                # interpret must not be the reason a mutation was allowed.
                return True
            if candidate == protected or protected in candidate.parents:
                return True
        return False

    def protected_matches(self, paths: Sequence[str]) -> list[str]:
        return [path for path in paths if self.is_path_protected(path)]

    def is_branch_protected(self, branch: str | None) -> bool:
        """Whether ``branch`` is protected, comparing normalised ref spellings both ways."""
        if branch is None:
            return False
        candidate = normalise_ref(branch)
        return any(candidate == normalise_ref(entry) for entry in self.protected_branches)

    # --------------------------------------------------------------------- evaluation

    def evaluate(
        self,
        operation: Operation,
        *,
        branch: str | None = None,
        paths: Sequence[str] = (),
    ) -> PolicyDecision:
        """Decide whether ``operation`` is permitted. Pure: no I/O, no model input."""
        # Guards that apply to every mutating operation, checked before the per-operation
        # flag so that no single `allow*` can override them.
        if operation in _MUTATING:
            offending = self.protected_matches(paths)
            if offending:
                return PolicyDecision(
                    operation,
                    False,
                    rule="protected_paths",
                    reason="operation touches a protected path",
                    detail={"paths": offending},
                )
            if self.is_branch_protected(branch):
                return PolicyDecision(
                    operation,
                    False,
                    rule="protected_branch",
                    reason="operation would mutate a protected or default branch",
                    detail={"branch": branch},
                )

        handler: PolicyRule | None = _RULES.get(operation)
        if handler is None:
            # Unreachable while the table is exhaustive, and deliberately a denial rather
            # than a KeyError: a new operation must fail shut, not crash open.
            return PolicyDecision(
                operation,
                False,
                rule="unclassified_operation",
                reason="no policy rule classifies this operation, so it is denied",
            )
        return handler(self, operation, branch, paths)


# ======================================================================================
# Rules
# ======================================================================================


def _always_allow(
    _policy: SafetyPolicy, operation: Operation, _branch: str | None, _paths: Sequence[str]
) -> PolicyDecision:
    return PolicyDecision(
        operation, True, rule="read_only", reason="read-only operations are always permitted"
    )


def _never_allow(reason: str, rule: str) -> PolicyRule:
    def rule_fn(
        _policy: SafetyPolicy, operation: Operation, _branch: str | None, _paths: Sequence[str]
    ) -> PolicyDecision:
        return PolicyDecision(operation, False, rule=rule, reason=reason)

    return rule_fn


def _flag(attribute: str, rule: str, noun: str) -> PolicyRule:
    def rule_fn(
        policy: SafetyPolicy, operation: Operation, _branch: str | None, _paths: Sequence[str]
    ) -> PolicyDecision:
        allowed = bool(getattr(policy, attribute))
        return PolicyDecision(
            operation,
            allowed,
            rule=rule,
            reason=(
                f"{noun} is permitted by configuration"
                if allowed
                else f"{noun} is not permitted; set safety.{_config_key(attribute)} to enable it"
            ),
        )

    return rule_fn


def _config_key(attribute: str) -> str:
    head, _, tail = attribute.partition("_")
    return head + tail.title() if tail else head


def _local_mutation(
    policy: SafetyPolicy, operation: Operation, _branch: str | None, _paths: Sequence[str]
) -> PolicyDecision:
    """Branch/worktree/file changes ride on ``allowCommit``.

    Producing local changes that could never be committed would be work Claude Away is not
    allowed to finish, so the two are gated together rather than leaving a mode that
    generates unusable diffs.
    """
    allowed = policy.allow_commit
    return PolicyDecision(
        operation,
        allowed,
        rule="allow_commit",
        reason=(
            "local mutation is permitted by configuration"
            if allowed
            else "local mutation is not permitted; set safety.allowCommit to enable it"
        ),
    )


_RULES: dict[Operation, PolicyRule] = {
    Operation.INSPECT: _always_allow,
    Operation.READ_FILE: _always_allow,
    Operation.CREATE_BRANCH: _local_mutation,
    Operation.CREATE_WORKTREE: _local_mutation,
    Operation.WRITE_FILE: _local_mutation,
    Operation.COMMIT: _flag("allow_commit", "allow_commit", "committing"),
    Operation.PUSH: _flag("allow_push", "allow_push", "pushing"),
    Operation.OPEN_PULL_REQUEST: _flag("allow_push", "allow_push", "opening a pull request"),
    Operation.MERGE: _flag("allow_merge", "allow_merge", "merging"),
    Operation.DEPLOY: _flag("allow_deploy", "allow_deploy", "deploying"),
    Operation.DELETE_BRANCH: _flag("allow_destructive", "allow_destructive", "deleting a branch"),
    Operation.DESTRUCTIVE: _flag(
        "allow_destructive", "allow_destructive", "destructive operations"
    ),
    # No configuration key grants these, and none should be added without a design
    # discussion. A force push destroys history that somebody else may already have pulled,
    # and history rewriting does the same to work Claude Away itself may have relied on.
    # There is deliberately no flag to infer them from: `allowPush` is about publishing
    # commits, not about discarding other people's.
    Operation.FORCE_PUSH: _never_allow(
        "force pushing is never permitted; no configuration grants it and it cannot be "
        "inferred from allowPush",
        "force_push_never_permitted",
    ),
    Operation.REWRITE_HISTORY: _never_allow(
        "history rewriting is never permitted while unattended",
        "rewrite_history_never_permitted",
    ),
}
