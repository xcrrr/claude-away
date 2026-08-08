"""Core value types for the deterministic state core.

These are the vocabulary the rest of the package speaks. They are deliberately plain --
frozen dataclasses and enums, no ORM, no inheritance hierarchy -- because the storage
layer is the authority and these types are just a typed view onto it.

The transition table in this module is a direct transcription of ``docs/STATE_MODEL.md``.
If the two ever disagree, that is a bug; ``tests/unit/test_state_model_doc_parity.py``
parses the document and asserts they match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ATTEMPT_ID_PATTERN",
    "TASK_ID_PATTERN",
    "TERMINAL_STATUSES",
    "VERIFICATION_ID_PATTERN",
    "AttemptOutcome",
    "EstimatedEffort",
    "EvidenceRecord",
    "EvidenceResult",
    "EvidenceType",
    "LeaseRecord",
    "Risk",
    "Task",
    "TaskAttempt",
    "TaskStatus",
    "VerificationRequirement",
    "VerificationType",
    "is_transition_allowed",
]


TASK_ID_PATTERN = re.compile(r"^AWAY-[0-9]{4,}$")
"""Task identifiers are stable and human-readable, e.g. ``AWAY-0001``."""

VERIFICATION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
"""Verification requirement identifiers are stable within a task, e.g. ``unit-tests``."""

ATTEMPT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""Attempt identifiers are opaque 32-character hex strings."""


class TaskStatus(str, Enum):
    """The eight documented task states.

    Inheriting from ``str`` keeps these directly usable as SQLite values and JSON output
    without a conversion layer, while still giving us exhaustiveness checking in mypy.
    """

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
"""States from which no further progress happens without creating a new task.

``BLOCKED`` is deliberately *not* terminal: a blocker can be resolved, which returns the
task to ``READY``.
"""


# The authoritative transition table, transcribed from docs/STATE_MODEL.md.
#
# Note the asymmetry that matters most: DONE has no outgoing edges at all. If later work
# invalidates a completed result, the documented remedy is a *follow-up task* linked to
# the original evidence -- never a silent reopening of history. CANCELLED is reachable
# from every non-DONE state, which is why it is added programmatically below rather than
# repeated in every row.
_BASE_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING}),
    TaskStatus.RUNNING: frozenset({TaskStatus.VERIFYING, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.DONE, TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.FAILED}
    ),
    TaskStatus.DONE: frozenset(),
    # BLOCKED -> PENDING is the one edge this table adds beyond the original document.
    # See BLOCKED_TO_PENDING_RATIONALE below; docs/STATE_MODEL.md now carries the same row.
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.PENDING}),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

BLOCKED_TO_PENDING_RATIONALE = (
    "Resolving a blocker must recompute readiness rather than assume it. While a task sat "
    "BLOCKED a dependency may have been CANCELLED, so promoting straight to READY could "
    "start a task whose prerequisites will never be satisfied. The original transition "
    "table listed only BLOCKED -> READY, which is unsound in that case."
)
"""Why the implemented table contains one edge the v0.0.1 document did not.

Kept as a named constant so the parity test can assert that every divergence from
``docs/STATE_MODEL.md`` is deliberate and explained, rather than accumulating silently.
"""


def _build_transition_table() -> dict[TaskStatus, frozenset[TaskStatus]]:
    table: dict[TaskStatus, frozenset[TaskStatus]] = {}
    for status, targets in _BASE_TRANSITIONS.items():
        if status is TaskStatus.DONE:
            # "No transition out of DONE occurs silently." DONE is absorbing, and that
            # includes cancellation: cancelling completed work would erase evidence.
            table[status] = targets
        elif status is TaskStatus.CANCELLED:
            table[status] = targets
        else:
            table[status] = targets | {TaskStatus.CANCELLED}
    return table


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = _build_transition_table()
"""``{from_status: {allowed to_status, ...}}`` -- the complete transition contract."""


def is_transition_allowed(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """Return whether ``from_status -> to_status`` appears in the transition table.

    This is a *structural* check only. Semantic guards (dependencies satisfied, evidence
    gate passed, lease held) live in :mod:`claude_away.core.state`; a transition must
    clear both to be applied.
    """
    return to_status in ALLOWED_TRANSITIONS[from_status]


class Risk(str, Enum):
    """How much damage a task could plausibly do if it goes wrong."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EstimatedEffort(str, Enum):
    """Coarse effort classes. Deliberately not hours: nobody can estimate hours."""

    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class VerificationType(str, Enum):
    """The kinds of acceptance check a task can declare.

    Milestone 1 models and enforces these; it does not execute them. Execution belongs to
    Milestone 3, and keeping the two separate is what lets us test the gate exhaustively
    without shelling out to anything.
    """

    COMMAND = "command"
    ARTIFACT = "artifact"
    GIT = "git"
    REVIEW = "review"
    MANUAL = "manual"


class EvidenceType(str, Enum):
    """The kinds of evidence that can be recorded against a verification requirement."""

    COMMAND = "command"
    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"
    ARTIFACT = "artifact"
    GIT = "git"
    REVIEW = "review"
    PULL_REQUEST = "pull_request"
    MANUAL_APPROVAL = "manual_approval"


class EvidenceResult(str, Enum):
    """The outcome of a single recorded piece of evidence.

    ``ERROR`` is distinct from ``FAIL``: a check that could not run (missing interpreter,
    permission denied) is not the same as a check that ran and rejected the work. Neither
    satisfies the gate, but the supervisor should treat them differently when deciding
    whether a retry is worthwhile.
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"


class AttemptOutcome(str, Enum):
    """How an attempt ended. ``None`` on the record means the attempt is still active."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
    INTERRUPTED = "interrupted"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    """A single declared acceptance check.

    ``id`` is stable *within a task* and is what evidence points at. It replaces the
    array-position identity used in the v0.0.1 schema, which could not survive a replan
    reordering the list.

    ``spec_hash`` is a content hash of the requirement's meaningful fields. It is the
    mechanism that stops evidence recorded against an older definition of a requirement
    from silently satisfying a newer, different definition of the same ``id``.
    """

    id: str
    type: VerificationType
    required: bool
    spec_hash: str
    command: str | None = None
    path: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not VERIFICATION_ID_PATTERN.match(self.id):
            raise ValueError(f"invalid verification id {self.id!r}")


@dataclass(frozen=True, slots=True)
class Task:
    """A unit of work the deterministic core tracks."""

    id: str
    project_id: str
    title: str
    description: str
    status: TaskStatus
    priority: int
    risk: Risk
    estimated_effort: EstimatedEffort
    human_required: bool
    created_by_plan_version: int
    updated_by_plan_version: int
    created_at: datetime
    updated_at: datetime
    goal_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    verification: tuple[VerificationRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not TASK_ID_PATTERN.match(self.id):
            raise ValueError(f"invalid task id {self.id!r}")


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """One execution attempt against a task.

    Terminal attempts are immutable history. While an attempt is active, only an
    explicitly bounded set of recovery fields may change (heartbeat and checkpoint) --
    see :mod:`claude_away.core.attempts` for exactly which, and why that does not weaken
    the immutability guarantee.
    """

    id: str
    task_id: str
    attempt_number: int
    runner_id: str
    mode: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: AttemptOutcome | None = None
    failure_reason: str | None = None
    base_commit: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    session_id: str | None = None
    workflow_id: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    heartbeat_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.outcome is None


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One append-only piece of evidence.

    ``requirement_spec_hash`` is captured at record time. The gate compares it against the
    requirement's current hash, so editing a requirement invalidates evidence gathered
    under its previous definition instead of silently inheriting it.
    """

    id: int
    task_id: str
    attempt_id: str | None
    verification_id: str | None
    requirement_spec_hash: str | None
    type: EvidenceType
    result: EvidenceResult
    summary: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """An execution lease over a task."""

    id: int
    task_id: str
    owner_id: str
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    renewed_at: datetime | None = None
    fence: int = 0

    def is_live_at(self, moment: datetime) -> bool:
        """A lease is live when it has not been released and has not yet expired."""
        return self.released_at is None and moment < self.expires_at
