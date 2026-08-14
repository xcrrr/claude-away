"""Establishing an unambiguous base revision, or refusing to.

Before a later milestone branches from a repository, the controller must be able to say
exactly which commit that branch will start from. This module answers that question, and
its more important job is answering *no* clearly.

Every refusal below is a state where a reasonable-looking guess exists and is wrong:

* a detached HEAD looks like a fine starting point until the work is orphaned;
* a dirty worktree looks fine until somebody else's uncommitted changes are committed
  under a task's name;
* an interrupted rebase looks fine until the new branch is cut from a half-applied series;
* an unknown default branch looks fine until the "default" turns out to be protected.

So the return type is a verdict, not a commit. `Claude thinks this is probably okay` is
exactly the sentence this module exists to make unrepresentable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from claude_away.adapters.git import RepositoryInspection, resolve_local_ref

__all__ = [
    "BaseRefusal",
    "BaseResolution",
    "resolve_expected_base",
]


class BaseRefusal(str, Enum):
    """Why a base revision could not be established. The supervisor branches on this."""

    UNBORN_HEAD = "unborn_head"
    DETACHED_HEAD = "detached_head"
    DIRTY_WORKTREE = "dirty_worktree"
    UNMERGED_PATHS = "unmerged_paths"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    DIRTY_SUBMODULES = "dirty_submodules"
    UNVERIFIABLE_PATHS = "unverifiable_paths"
    UNKNOWN_DEFAULT_BRANCH = "unknown_default_branch"
    MISSING_REF = "missing_ref"
    UNEXPECTED_BRANCH = "unexpected_branch"
    DIVERGED_FROM_EXPECTED = "diverged_from_expected"


@dataclass(frozen=True, slots=True)
class BaseResolution:
    """The verdict. Either a commit worth branching from, or the reasons it is not."""

    project_id: str
    resolved: bool
    commit: str | None = None
    branch: str | None = None
    refusals: tuple[BaseRefusal, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "resolved": self.resolved,
            "commit": self.commit,
            "branch": self.branch,
            "refusals": [refusal.value for refusal in self.refusals],
            "detail": dict(self.detail),
        }


def resolve_expected_base(
    inspection: RepositoryInspection,
    *,
    project_id: str,
    expected_branch: str | None = None,
    expected_commit: str | None = None,
    require_clean: bool = True,
) -> BaseResolution:
    """Decide whether ``inspection`` yields a base revision safe to branch from.

    ``expected_branch`` defaults to the repository's default branch. ``expected_commit``, if
    given, must match what that branch actually points at -- the check that catches a local
    branch which has moved since the plan was made.

    All refusals are collected rather than short-circuiting, because an operator fixing one
    problem should not have to re-run to discover the next.
    """
    refusals: list[BaseRefusal] = []
    detail: dict[str, Any] = {}

    if inspection.is_unborn:
        # No commit exists at all; there is nothing to branch from.
        return BaseResolution(
            project_id=project_id,
            resolved=False,
            refusals=(BaseRefusal.UNBORN_HEAD,),
            detail={"root": str(inspection.root)},
        )

    if inspection.operations_in_progress:
        refusals.append(BaseRefusal.OPERATION_IN_PROGRESS)
        detail["operations_in_progress"] = [
            operation.value for operation in inspection.operations_in_progress
        ]

    status = inspection.status
    if status.unmerged:
        refusals.append(BaseRefusal.UNMERGED_PATHS)
        detail["unmerged"] = list(status.unmerged)

    if require_clean and (status.staged or status.unstaged or status.untracked):
        refusals.append(BaseRefusal.DIRTY_WORKTREE)
        detail["staged"] = list(status.staged)
        detail["unstaged"] = list(status.unstaged)
        detail["untracked"] = list(status.untracked)

    if require_clean and status.dirty_submodules:
        refusals.append(BaseRefusal.DIRTY_SUBMODULES)
        detail["dirty_submodules"] = [module.path for module in status.dirty_submodules]

    if require_clean and status.unverifiable:
        # Its own refusal rather than folding into DIRTY_WORKTREE, because the operator's
        # next action is different and not guessable from "dirty": these paths may well be
        # unmodified, but `git update-index --assume-unchanged` / `--skip-worktree` has told
        # Git not to look, so nothing can establish that they are. The message says which
        # command undoes it.
        refusals.append(BaseRefusal.UNVERIFIABLE_PATHS)
        detail["unverifiable"] = list(status.unverifiable)
        detail["unverifiable_hint"] = (
            "these paths are marked assume-unchanged or skip-worktree, so `git status` "
            "cannot report whether they differ from HEAD; clear the bit with "
            "`git update-index --no-assume-unchanged <path>` or `--no-skip-worktree <path>`"
        )

    target_branch = expected_branch or inspection.default_branch
    if target_branch is None:
        # Refusing beats guessing "main": a wrong guess here is a guess about which branch
        # is protected, and the failure mode is committing to it.
        refusals.append(BaseRefusal.UNKNOWN_DEFAULT_BRANCH)
        detail["hint"] = (
            "set defaultBranch for this project, or ensure refs/remotes/origin/HEAD exists"
        )
        return BaseResolution(
            project_id=project_id, resolved=False, refusals=tuple(refusals), detail=detail
        )

    if inspection.is_detached:
        refusals.append(BaseRefusal.DETACHED_HEAD)
        detail["head_commit"] = inspection.head_commit
    elif inspection.branch != target_branch:
        refusals.append(BaseRefusal.UNEXPECTED_BRANCH)
        detail["current_branch"] = inspection.branch
        detail["expected_branch"] = target_branch

    branch_commit = resolve_local_ref(inspection.root, target_branch)
    if branch_commit is None:
        # Deliberately no fetch. A missing local ref is a fact for the caller to act on,
        # not something to repair behind their back.
        refusals.append(BaseRefusal.MISSING_REF)
        detail["missing_ref"] = target_branch
        return BaseResolution(
            project_id=project_id,
            resolved=False,
            branch=target_branch,
            refusals=tuple(refusals),
            detail=detail,
        )

    if expected_commit is not None and expected_commit != branch_commit:
        refusals.append(BaseRefusal.DIVERGED_FROM_EXPECTED)
        detail["expected_commit"] = expected_commit
        detail["actual_commit"] = branch_commit

    if refusals:
        return BaseResolution(
            project_id=project_id,
            resolved=False,
            branch=target_branch,
            refusals=tuple(refusals),
            detail=detail,
        )

    return BaseResolution(
        project_id=project_id, resolved=True, commit=branch_commit, branch=target_branch
    )
