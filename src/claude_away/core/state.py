"""The authoritative task state machine.

Every status change in Claude Away passes through :func:`_transition` in this module.
That is not a convention -- the database refuses any other route. ``tasks.status`` is
guarded by a trigger that calls a function registered only on connections owned by
:class:`~claude_away.core.db.Database`, and :func:`_transition` is the only caller that
opens the gate. A connection that has not registered the function (a stray script, the
``sqlite3`` CLI, a future contributor's debugging session) cannot change a status at all.

Each transition is checked twice before it is applied:

1. **Structurally**, against the table transcribed from ``docs/STATE_MODEL.md``. Is this
   edge in the graph at all?
2. **Semantically**, against a guard specific to the edge. Are the dependencies actually
   ``DONE``? Does the caller hold the lease? Has the evidence gate opened?

Both must pass. Status, event and any attempt bookkeeping are written in one transaction,
so a process that dies mid-transition leaves the previous state intact rather than a task
that is ``DONE`` with no evidence row or ``RUNNING`` with no attempt.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from claude_away.clock import parse_timestamp, to_iso
from claude_away.core.dag import blocking_dependencies, compute_ready
from claude_away.core.db import Database, dumps, loads
from claude_away.core.evidence import GateReport, evaluate_gate
from claude_away.core.models import (
    AttemptOutcome,
    TaskAttempt,
    TaskStatus,
    is_transition_allowed,
)
from claude_away.core.repository import task_nodes
from claude_away.errors import (
    EvidenceIncompleteError,
    IdempotencyConflictError,
    IllegalTransitionError,
    InvalidStateError,
    LeaseNotHeldError,
    NotFoundError,
    StaleReplayError,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "TransitionResult",
    "active_attempt",
    "begin_verification",
    "cancel_task",
    "checkpoint_attempt",
    "fail_task",
    "list_attempts",
    "mark_blocked",
    "mark_verified",
    "refresh_readiness",
    "request_retry",
    "resolve_blocker",
    "start_attempt",
    "suspend_attempt",
]

DEFAULT_MAX_ATTEMPTS = 3
"""Mirrors ``execution.maxAttemptsPerTask`` in the config schema.

Bounded retries are a promise the README makes. Once the budget is spent a task becomes
``FAILED`` rather than looping, because an unattended system that retries forever is
indistinguishable from one that is stuck.
"""


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What a transition did. Returned rather than logged so callers can assert on it."""

    task_id: str
    from_status: TaskStatus
    to_status: TaskStatus
    event_id: int
    attempt_id: str | None = None
    replayed: bool = False
    """``True`` when an idempotency key matched an earlier identical call."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "event_id": self.event_id,
            "attempt_id": self.attempt_id,
            "replayed": self.replayed,
        }


# ======================================================================================
# Idempotency
# ======================================================================================


def _request_fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{operation}:{dumps(dict(payload))}".encode()).hexdigest()


def _check_idempotency(
    con: Any, key: str | None, operation: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return a previously recorded result for ``key``, or ``None`` to proceed.

    Replaying an identical call after a crash returns the original outcome. Reusing a key
    with *different* content is a caller bug and raises rather than silently picking one.

    Takes a connection rather than the :class:`Database` so the lookup runs inside the same
    transaction as the effect it guards. Checking first and writing afterwards would let two
    concurrent replays of one key both pass the check; the second would then collide on the
    primary key and surface a raw constraint error instead of the replay it asked for.
    """
    if key is None:
        return None
    row = con.execute(
        "SELECT operation, request_hash, result FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    fingerprint = _request_fingerprint(operation, payload)
    if str(row["operation"]) != operation or str(row["request_hash"]) != fingerprint:
        raise IdempotencyConflictError(key=key, operation=operation)
    return loads(str(row["result"]))


def _replay_result(
    con: Any,
    task_id: str,
    recorded: Mapping[str, Any],
    *,
    key: str,
    operation: str,
    require_active_attempt: bool,
) -> TransitionResult:
    """Return a recorded outcome, but only if it still describes reality.

    A replay answers "did my earlier call land?", not "pretend it is still true". Between
    the original call and the replay the world can have moved on -- the task may have been
    failed or cancelled, the attempt closed, the lease lost. Handing back a cheerful
    ``READY -> RUNNING`` for a task that is now ``FAILED`` would tell a recovering
    supervisor it owns work that it does not.

    ``require_active_attempt`` distinguishes the two shapes: ``start_attempt`` promises a
    live attempt, so a closed one invalidates the replay, whereas ``mark_verified``
    deliberately closes the attempt it reports.
    """
    recorded_status = TaskStatus(str(recorded["to_status"]))
    current_status = _load_status(con, task_id)
    if current_status is not recorded_status:
        raise StaleReplayError(
            "recorded result no longer describes the task's current state",
            key=key,
            operation=operation,
            task_id=task_id,
            recorded_status=recorded_status.value,
            current_status=current_status.value,
        )

    attempt_id = recorded.get("attempt_id")
    if require_active_attempt and attempt_id is not None:
        still_active = con.execute(
            "SELECT 1 FROM attempts WHERE id = ? AND outcome IS NULL", (attempt_id,)
        ).fetchone()
        if still_active is None:
            raise StaleReplayError(
                "the attempt this result refers to is no longer active",
                key=key,
                operation=operation,
                task_id=task_id,
                attempt_id=str(attempt_id),
            )

    return TransitionResult(
        task_id=task_id,
        from_status=TaskStatus(str(recorded["from_status"])),
        to_status=TaskStatus(str(recorded["to_status"])),
        event_id=int(recorded["event_id"]),
        attempt_id=recorded.get("attempt_id"),
        replayed=True,
    )


def _store_idempotency(
    db: Database,
    con: Any,
    key: str | None,
    operation: str,
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    if key is None:
        return
    con.execute(
        "INSERT INTO idempotency_keys(key, operation, request_hash, result, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            key,
            operation,
            _request_fingerprint(operation, payload),
            dumps(dict(result)),
            to_iso(db.clock.now()),
        ),
    )


# ======================================================================================
# Core transition
# ======================================================================================


def _load_status(con: Any, task_id: str) -> TaskStatus:
    row = con.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError("task", task_id)
    return TaskStatus(str(row["status"]))


def _require_lease(db: Database, con: Any, task_id: str, owner_id: str) -> None:
    """Refuse a runner-initiated transition unless the caller holds a live lease."""
    row = con.execute(
        "SELECT owner_id, expires_at, released_at FROM leases "
        " WHERE task_id = ? AND released_at IS NULL",
        (task_id,),
    ).fetchone()
    if row is None:
        raise LeaseNotHeldError(
            "operation requires a live lease on the task", task_id=task_id, owner_id=owner_id
        )
    if str(row["owner_id"]) != owner_id:
        raise LeaseNotHeldError(
            "task is leased by a different owner",
            task_id=task_id,
            holder=str(row["owner_id"]),
            requester=owner_id,
        )
    if parse_timestamp(str(row["expires_at"])) <= db.clock.now():
        raise LeaseNotHeldError(
            "lease has expired; reconcile before continuing",
            task_id=task_id,
            owner_id=owner_id,
        )


def _transition(
    db: Database,
    con: Any,
    task_id: str,
    to_status: TaskStatus,
    *,
    kind: str,
    actor: str,
    attempt_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Apply one status change. The only place ``tasks.status`` is ever written."""
    from_status = _load_status(con, task_id)

    if not is_transition_allowed(from_status, to_status):
        raise IllegalTransitionError(
            task_id=task_id,
            from_status=from_status.value,
            to_status=to_status.value,
            reason="edge is not in the documented transition table",
        )

    now = to_iso(db.clock.now())
    con.execute(
        "UPDATE tasks SET status = ?, updated_at = ?, status_changed_at = ? WHERE id = ?",
        (to_status.value, now, now, task_id),
    )
    cursor = con.execute(
        "INSERT INTO events("
        "  created_at, kind, task_id, attempt_id, from_status, to_status, actor, payload"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            kind,
            task_id,
            attempt_id,
            from_status.value,
            to_status.value,
            actor,
            dumps(dict(payload or {})),
        ),
    )
    return TransitionResult(
        task_id=task_id,
        from_status=from_status,
        to_status=to_status,
        event_id=int(cursor.lastrowid or 0),
        attempt_id=attempt_id,
    )


# ======================================================================================
# Readiness
# ======================================================================================


def refresh_readiness(db: Database, *, actor: str = "system") -> list[str]:
    """Promote every eligible ``PENDING`` task to ``READY``.

    Deterministic and order-independent: the set of promotions depends only on the current
    statuses, so running it twice in a row promotes nothing the second time. Safe to call
    after any terminal event, and safe to call again after a crash.
    """
    promoted: list[str] = []
    with db._status_transition() as con:
        for task_id in compute_ready(task_nodes(db)):
            _transition(
                db,
                con,
                task_id,
                TaskStatus.READY,
                kind="task_ready",
                actor=actor,
                payload={"trigger": "dependencies_satisfied"},
            )
            promoted.append(task_id)
    return promoted


# ======================================================================================
# Attempts
# ======================================================================================


def active_attempt(db: Database, task_id: str) -> TaskAttempt | None:
    row = db.query_one("SELECT * FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,))
    return None if row is None else _row_to_attempt(row)


def list_attempts(db: Database, task_id: str) -> list[TaskAttempt]:
    return [
        _row_to_attempt(row)
        for row in db.query(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number", (task_id,)
        )
    ]


def _row_to_attempt(row: Any) -> TaskAttempt:
    return TaskAttempt(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        attempt_number=int(row["attempt_number"]),
        runner_id=str(row["runner_id"]),
        mode=str(row["mode"]),
        started_at=parse_timestamp(str(row["started_at"])),
        finished_at=(parse_timestamp(str(row["finished_at"])) if row["finished_at"] else None),
        outcome=AttemptOutcome(str(row["outcome"])) if row["outcome"] else None,
        failure_reason=row["failure_reason"],
        base_commit=row["base_commit"],
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        session_id=row["session_id"],
        workflow_id=row["workflow_id"],
        checkpoint=loads(row["checkpoint"]),
        heartbeat_at=(parse_timestamp(str(row["heartbeat_at"])) if row["heartbeat_at"] else None),
    )


def _open_attempt(
    db: Database,
    con: Any,
    task_id: str,
    *,
    runner_id: str,
    mode: str,
    base_commit: str | None,
    branch: str | None,
) -> str:
    row = con.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM attempts WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    attempt_number = int(row["n"])
    attempt_id = uuid.uuid4().hex
    now = to_iso(db.clock.now())
    con.execute(
        "INSERT INTO attempts("
        "  id, task_id, attempt_number, runner_id, mode, started_at, base_commit, branch,"
        "  heartbeat_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (attempt_id, task_id, attempt_number, runner_id, mode, now, base_commit, branch, now),
    )
    return attempt_id


def _close_attempt(
    db: Database,
    con: Any,
    attempt_id: str,
    outcome: AttemptOutcome,
    *,
    failure_reason: str | None = None,
) -> None:
    con.execute(
        "UPDATE attempts SET outcome = ?, finished_at = ?, failure_reason = ? WHERE id = ?",
        (outcome.value, to_iso(db.clock.now()), failure_reason, attempt_id),
    )


def checkpoint_attempt(
    db: Database,
    task_id: str,
    *,
    owner_id: str,
    checkpoint: Mapping[str, Any] | None = None,
) -> TaskAttempt:
    """Record a heartbeat and optionally a recovery checkpoint on the live attempt.

    This is the bounded exception to attempt immutability, and the boundary is enforced by
    a trigger: only ``heartbeat_at`` and ``checkpoint`` may move, only while the attempt is
    open, and never once it has an outcome. Identity, timing and provenance are immutable
    from the moment the row is written.
    """
    with db.transaction() as con:
        _require_lease(db, con, task_id, owner_id)
        row = con.execute(
            "SELECT id, checkpoint FROM attempts WHERE task_id = ? AND outcome IS NULL",
            (task_id,),
        ).fetchone()
        if row is None:
            raise InvalidStateError("no active attempt to checkpoint", task_id=task_id)

        merged = loads(row["checkpoint"])
        merged.update(dict(checkpoint or {}))
        con.execute(
            "UPDATE attempts SET heartbeat_at = ?, checkpoint = ? WHERE id = ?",
            (to_iso(db.clock.now()), dumps(merged), str(row["id"])),
        )

    refreshed = active_attempt(db, task_id)
    assert refreshed is not None
    return refreshed


# ======================================================================================
# Runner-driven transitions
# ======================================================================================


def start_attempt(
    db: Database,
    task_id: str,
    *,
    owner_id: str,
    mode: str = "local",
    base_commit: str | None = None,
    branch: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    idempotency_key: str | None = None,
) -> TransitionResult:
    """``READY -> RUNNING``, opening a new attempt.

    A task cannot be ``RUNNING`` without an attempt: the two are written in one
    transaction, so there is no window in which a crash could leave a running task with no
    record of who was running it.
    """
    operation = "start_attempt"
    payload = {"task_id": task_id, "owner_id": owner_id, "mode": mode}

    with db._status_transition() as con:
        recorded = _check_idempotency(con, idempotency_key, operation, payload)
        if recorded is not None:
            return _replay_result(
                con,
                task_id,
                recorded,
                key=str(idempotency_key),
                operation=operation,
                require_active_attempt=True,
            )

        _require_lease(db, con, task_id, owner_id)

        used = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()["n"]
        )
        if used >= max_attempts:
            raise InvalidStateError(
                "attempt budget exhausted for this task",
                task_id=task_id,
                attempts_used=used,
                max_attempts=max_attempts,
            )

        attempt_id = _open_attempt(
            db,
            con,
            task_id,
            runner_id=owner_id,
            mode=mode,
            base_commit=base_commit,
            branch=branch,
        )
        result = _transition(
            db,
            con,
            task_id,
            TaskStatus.RUNNING,
            kind="attempt_started",
            actor=owner_id,
            attempt_id=attempt_id,
            payload={"mode": mode, "base_commit": base_commit, "branch": branch},
        )
        _store_idempotency(db, con, idempotency_key, operation, payload, result.to_dict())
    return result


def begin_verification(db: Database, task_id: str, *, owner_id: str) -> TransitionResult:
    """``RUNNING -> VERIFYING``. The attempt stays open; verification belongs to it."""
    with db._status_transition() as con:
        _require_lease(db, con, task_id, owner_id)
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            raise InvalidStateError("cannot verify a task with no active attempt", task_id=task_id)
        return _transition(
            db,
            con,
            task_id,
            TaskStatus.VERIFYING,
            kind="verification_started",
            actor=owner_id,
            attempt_id=str(row["id"]),
        )


def mark_verified(
    db: Database,
    task_id: str,
    *,
    owner_id: str,
    idempotency_key: str | None = None,
) -> TransitionResult:
    """``VERIFYING -> DONE``, but only if the evidence gate opens.

    This is the function the whole milestone exists to make trustworthy. The gate is
    evaluated *inside* the same transaction that writes the status, so no interleaving
    call can invalidate the evidence between the check and the write.

    The resulting :class:`~claude_away.core.evidence.GateReport` is stored on the event as
    a seal: months later the ledger still shows exactly which evidence justified the
    completion, rather than merely that somebody decided it was complete.
    """
    operation = "mark_verified"
    payload = {"task_id": task_id, "owner_id": owner_id}

    with db._status_transition() as con:
        recorded = _check_idempotency(con, idempotency_key, operation, payload)
        if recorded is not None:
            return _replay_result(
                con,
                task_id,
                recorded,
                key=str(idempotency_key),
                operation=operation,
                require_active_attempt=False,
            )

        _require_lease(db, con, task_id, owner_id)
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            raise InvalidStateError(
                "cannot complete a task with no active attempt; a suspended or closed "
                "attempt means nothing produced evidence for this run",
                task_id=task_id,
            )
        attempt_id = str(row["id"])

        report: GateReport = evaluate_gate(db, task_id, attempt_id=attempt_id)
        if not report.satisfied:
            raise EvidenceIncompleteError(
                task_id=task_id,
                attempt_id=attempt_id,
                missing=list(report.missing),
                failed=list(report.failed),
            )

        if attempt_id is not None:
            _close_attempt(db, con, attempt_id, AttemptOutcome.SUCCEEDED)

        result = _transition(
            db,
            con,
            task_id,
            TaskStatus.DONE,
            kind="task_completed",
            actor=owner_id,
            attempt_id=attempt_id,
            payload={"gate": report.to_dict()},
        )
        _store_idempotency(db, con, idempotency_key, operation, payload, result.to_dict())
    return result


def request_retry(
    db: Database,
    task_id: str,
    *,
    owner_id: str,
    reason: str,
    mode: str = "local",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> TransitionResult:
    """``VERIFYING -> RUNNING``: close the failed attempt and open a fresh one.

    Closing the attempt is what makes stale-evidence protection automatic. The runner is
    about to change the code, so every evidence row from the previous attempt describes a
    different artifact; scoping the gate to the new attempt discards them all without any
    special-case logic.

    When the attempt budget is spent the task goes to ``FAILED`` instead, which is how the
    README's "bounded retries, then BLOCKED or FAILED rather than infinite loops" is
    actually delivered.
    """
    with db._status_transition() as con:
        _require_lease(db, con, task_id, owner_id)
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            raise InvalidStateError("cannot retry a task with no active attempt", task_id=task_id)
        _close_attempt(db, con, str(row["id"]), AttemptOutcome.FAILED, failure_reason=reason)

        used = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()["n"]
        )
        if used >= max_attempts:
            return _transition(
                db,
                con,
                task_id,
                TaskStatus.FAILED,
                kind="retry_budget_exhausted",
                actor=owner_id,
                attempt_id=str(row["id"]),
                payload={"reason": reason, "attempts_used": used, "max_attempts": max_attempts},
            )

        attempt_id = _open_attempt(
            db, con, task_id, runner_id=owner_id, mode=mode, base_commit=None, branch=None
        )
        return _transition(
            db,
            con,
            task_id,
            TaskStatus.RUNNING,
            kind="verification_retry",
            actor=owner_id,
            attempt_id=attempt_id,
            payload={"reason": reason, "previous_attempt_id": str(row["id"])},
        )


def suspend_attempt(
    db: Database,
    task_id: str,
    *,
    owner_id: str,
    outcome: AttemptOutcome = AttemptOutcome.INTERRUPTED,
    reason: str = "",
) -> TaskAttempt | None:
    """Close the live attempt without changing the task's status.

    Used for rate limits, pauses and clean shutdowns. STATE_MODEL is explicit that a
    rate-limit interruption "closes or suspends an attempt cleanly; it does not create a
    fresh logical task" -- the task keeps its position in the graph and its evidence, and
    only the attempt ends. Critically, this never fabricates a completion: an interrupted
    task stays exactly as unfinished as it really is.
    """
    with db.transaction() as con:
        _require_lease(db, con, task_id, owner_id)
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            return None
        attempt_id = str(row["id"])
        _close_attempt(db, con, attempt_id, outcome, failure_reason=reason or None)
        con.execute(
            "INSERT INTO events(created_at, kind, task_id, attempt_id, actor, payload) "
            "VALUES (?, 'attempt_suspended', ?, ?, ?, ?)",
            (
                to_iso(db.clock.now()),
                task_id,
                attempt_id,
                owner_id,
                dumps({"outcome": outcome.value, "reason": reason}),
            ),
        )
        return _row_to_attempt(
            con.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        )


# ======================================================================================
# Supervisor and human transitions
# ======================================================================================


def mark_blocked(
    db: Database, task_id: str, *, reason: str, actor: str = "system"
) -> TransitionResult:
    """Move a task to ``BLOCKED``, closing any live attempt.

    ``BLOCKED`` is not terminal -- it is the honest answer when progress needs a human, a
    permission or an external service. Recording the reason is mandatory because a blocked
    task with no explanation is indistinguishable from a bug.
    """
    if not reason:
        raise ValueError("blocking a task requires a reason")
    with db._status_transition() as con:
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        attempt_id = None if row is None else str(row["id"])
        if attempt_id is not None:
            _close_attempt(db, con, attempt_id, AttemptOutcome.ABANDONED, failure_reason=reason)
        return _transition(
            db,
            con,
            task_id,
            TaskStatus.BLOCKED,
            kind="task_blocked",
            actor=actor,
            attempt_id=attempt_id,
            payload={"reason": reason},
        )


def resolve_blocker(
    db: Database, task_id: str, *, note: str, actor: str = "human"
) -> TransitionResult:
    """``BLOCKED -> READY`` (or ``PENDING`` if dependencies are no longer satisfied).

    Readiness is recomputed rather than assumed: while the task sat blocked, a dependency
    may have been cancelled or reopened, and promoting straight to ``READY`` would let a
    task run before its prerequisites.
    """
    with db._status_transition() as con:
        current = _load_status(con, task_id)
        if current is not TaskStatus.BLOCKED:
            raise IllegalTransitionError(
                task_id=task_id,
                from_status=current.value,
                to_status=TaskStatus.READY.value,
                reason="only a BLOCKED task can have its blocker resolved",
            )
        report = blocking_dependencies(task_id, task_nodes(db))
        target = TaskStatus.READY if report.is_satisfied else TaskStatus.PENDING
        return _transition(
            db,
            con,
            task_id,
            target,
            kind="blocker_resolved",
            actor=actor,
            payload={"note": note, "unsatisfied": list(report.unsatisfied)},
        )


def fail_task(
    db: Database, task_id: str, *, reason: str, actor: str = "system"
) -> TransitionResult:
    if not reason:
        raise ValueError("failing a task requires a reason")
    with db._status_transition() as con:
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        attempt_id = None if row is None else str(row["id"])
        if attempt_id is not None:
            _close_attempt(db, con, attempt_id, AttemptOutcome.FAILED, failure_reason=reason)
        return _transition(
            db,
            con,
            task_id,
            TaskStatus.FAILED,
            kind="task_failed",
            actor=actor,
            attempt_id=attempt_id,
            payload={"reason": reason},
        )


def cancel_task(
    db: Database, task_id: str, *, reason: str, actor: str = "human"
) -> TransitionResult:
    """Retire a task. Legal from every state except ``DONE``.

    Cancelling completed work is refused by the transition table *and* by a database
    trigger: ``DONE`` is absorbing, and erasing a completed result would destroy the
    evidence that justified it. The documented remedy is a follow-up task instead.
    """
    if not reason:
        raise ValueError("cancelling a task requires a reason")
    with db._status_transition() as con:
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        attempt_id = None if row is None else str(row["id"])
        if attempt_id is not None:
            _close_attempt(db, con, attempt_id, AttemptOutcome.ABANDONED, failure_reason=reason)
        return _transition(
            db,
            con,
            task_id,
            TaskStatus.CANCELLED,
            kind="task_cancelled",
            actor=actor,
            attempt_id=attempt_id,
            payload={"reason": reason},
        )
