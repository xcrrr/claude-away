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

from claude_away.errors import PolicyDeniedError

__all__ = [
    "Operation",
    "PolicyDecision",
    "PolicyRule",
    "SafetyPolicy",
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


def normalise_repo_path(path: str) -> PurePosixPath:
    """Normalise a repository-relative path for protected-path matching.

    Defined once, here, so that two call sites cannot disagree about whether ``./src`` and
    ``src`` are the same thing. Backslashes fold to forward slashes because a Windows-style
    path must not slip past a guard written with POSIX separators, and ``..`` is rejected
    outright: a protected-path check is meaningless if the path can escape the repository.
    """
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    candidate = PurePosixPath(cleaned)
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"repository-relative path must not contain '..': {path!r}")
    return candidate


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """A pure, testable view of ``config.safety``."""

    allow_commit: bool = True
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
        safety: Mapping[str, Any] = config.get("safety", {})
        return cls(
            allow_commit=bool(safety.get("allowCommit", False)),
            allow_push=bool(safety.get("allowPush", False)),
            allow_merge=bool(safety.get("allowMerge", False)),
            allow_deploy=bool(safety.get("allowDeploy", False)),
            allow_destructive=bool(safety.get("allowDestructive", False)),
            protected_paths=tuple(safety.get("protectedPaths", ()) or ()),
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
            except ValueError:
                continue
            if candidate == protected or protected in candidate.parents:
                return True
        return False

    def protected_matches(self, paths: Sequence[str]) -> list[str]:
        return [path for path in paths if self.is_path_protected(path)]

    def is_branch_protected(self, branch: str | None) -> bool:
        return branch is not None and branch in self.protected_branches

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
