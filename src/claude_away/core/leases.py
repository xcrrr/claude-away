"""Execution leases.

A lease is how exactly one runner comes to own a task. The guarantees, and the reasoning
behind the ones that are easy to get wrong:

**At most one live lease per task.** Enforced by a partial unique index
(``leases_one_active_per_task``), not by a check-then-insert. Two schedulers racing on
separate connections cannot both win, because the loser's ``INSERT`` violates the index.
The application-level check exists only to produce a good error message; the database is
what actually decides.

**Acquisition is a single ``BEGIN IMMEDIATE`` transaction.** Reading "is there a live
lease?" and then writing "there is now" is a classic read-then-write race. Under SQLite's
default deferred transactions both connections would take a read lock, and the second
attempt to upgrade fails with ``SQLITE_BUSY`` that ``busy_timeout`` cannot rescue. Taking
the write lock up front makes the sequence genuinely atomic.

**An expired lease does not mean "safe to rerun".** This is the rule most systems get
wrong. When a runner dies mid-task it may already have committed code, pushed a branch, or
started something external. Expiry therefore does *not* free the task: the lease row stays
unreleased and continues to occupy the unique-index slot. Taking over requires an explicit
:func:`reconcile_expired` call that records why the takeover is safe. A future scheduler
physically cannot grab a task just because the previous process disappeared.

**Fencing.** Each lease carries a per-task monotonic ``fence``. A process that was frozen
(swap, SIGSTOP, a laptop lid) and wakes up still holding a stale lease object can be
detected by comparing fences rather than by trusting wall-clock expiry alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from claude_away.clock import parse_timestamp, to_iso
from claude_away.core.db import Database
from claude_away.core.models import LeaseRecord
from claude_away.errors import (
    LeaseConflictError,
    LeaseExpiredError,
    LeaseNotHeldError,
    NotFoundError,
    ReconciliationRequiredError,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "LeaseAcquisition",
    "acquire_lease",
    "active_lease",
    "expired_leases",
    "heartbeat_lease",
    "reconcile_expired",
    "release_lease",
]

DEFAULT_LEASE_SECONDS = 300
"""Five minutes. Short enough that a dead runner is noticed promptly, long enough that a
slow task does not have to renew constantly. Renewal is the mechanism for long work."""


@dataclass(frozen=True, slots=True)
class LeaseAcquisition:
    """Result of acquiring a lease."""

    lease: LeaseRecord
    created: bool
    """``False`` when the caller already held a live lease and this was a safe replay."""


def _row_to_lease(row: object) -> LeaseRecord:
    assert row is not None
    mapping = row  # sqlite3.Row supports mapping access
    return LeaseRecord(
        id=int(mapping["id"]),  # type: ignore[index]
        task_id=str(mapping["task_id"]),  # type: ignore[index]
        owner_id=str(mapping["owner_id"]),  # type: ignore[index]
        acquired_at=parse_timestamp(str(mapping["acquired_at"])),  # type: ignore[index]
        expires_at=parse_timestamp(str(mapping["expires_at"])),  # type: ignore[index]
        released_at=(
            parse_timestamp(str(mapping["released_at"]))  # type: ignore[index]
            if mapping["released_at"] is not None  # type: ignore[index]
            else None
        ),
        renewed_at=(
            parse_timestamp(str(mapping["renewed_at"]))  # type: ignore[index]
            if mapping["renewed_at"] is not None  # type: ignore[index]
            else None
        ),
        fence=int(mapping["fence"]),  # type: ignore[index]
    )


_SELECT_LEASE = (
    "SELECT id, task_id, owner_id, acquired_at, expires_at, renewed_at, released_at, fence "
    "FROM leases"
)


def active_lease(db: Database, task_id: str) -> LeaseRecord | None:
    """Return the unreleased lease for a task, expired or not.

    Returning expired leases is the point: an expired lease is still an obstacle that must
    be reconciled, not a vacancy.
    """
    row = db.query_one(f"{_SELECT_LEASE} WHERE task_id = ? AND released_at IS NULL", (task_id,))
    return None if row is None else _row_to_lease(row)


def acquire_lease(
    db: Database,
    task_id: str,
    owner_id: str,
    *,
    duration_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeaseAcquisition:
    """Acquire a lease on ``task_id`` for ``owner_id``.

    Outcomes:

    * no live lease -> a new lease is created;
    * the *same* owner already holds a live lease -> safe replay, the existing lease is
      returned with ``created=False`` (this is what makes crash-restart idempotent);
    * a *different* owner holds a live lease -> :class:`LeaseConflictError`;
    * an unreleased lease has expired -> :class:`ReconciliationRequiredError`, regardless
      of who owns it, because the previous runner's side effects are unknown.
    """
    if duration_seconds <= 0:
        raise ValueError("lease duration must be positive")

    # `now` is sampled *inside* the transaction, after BEGIN IMMEDIATE has been granted.
    # Sampling before could mean waiting on the write lock and then writing an expiry
    # computed from a stale reading -- a lease that is already partly spent when issued.
    with db.transaction() as con:
        now = db.clock.now()
        expires_at = now + timedelta(seconds=duration_seconds)
        if con.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise NotFoundError("task", task_id)

        row = con.execute(
            f"{_SELECT_LEASE} WHERE task_id = ? AND released_at IS NULL", (task_id,)
        ).fetchone()

        if row is not None:
            existing = _row_to_lease(row)
            if not existing.is_live_at(now):
                raise ReconciliationRequiredError(
                    "task holds an expired lease that must be reconciled before it can run again",
                    task_id=task_id,
                    holder=existing.owner_id,
                    expired_at=to_iso(existing.expires_at),
                    requester=owner_id,
                )
            if existing.owner_id != owner_id:
                raise LeaseConflictError(
                    task_id=task_id,
                    holder=existing.owner_id,
                    requester=owner_id,
                    expires_at=to_iso(existing.expires_at),
                )
            # Same owner, still live: extend rather than duplicate. Re-acquiring after a
            # restart within the lease window is a normal recovery path, not an error.
            con.execute(
                "UPDATE leases SET expires_at = ?, renewed_at = ? WHERE id = ?",
                (to_iso(expires_at), to_iso(now), existing.id),
            )
            refreshed = con.execute(f"{_SELECT_LEASE} WHERE id = ?", (existing.id,)).fetchone()
            return LeaseAcquisition(lease=_row_to_lease(refreshed), created=False)

        fence_row = con.execute(
            "SELECT COALESCE(MAX(fence), 0) + 1 AS next FROM leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        fence = int(fence_row["next"])

        cursor = con.execute(
            "INSERT INTO leases(task_id, owner_id, acquired_at, expires_at, fence) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, owner_id, to_iso(now), to_iso(expires_at), fence),
        )
        created = con.execute(
            f"{_SELECT_LEASE} WHERE id = ?", (int(cursor.lastrowid or 0),)
        ).fetchone()
        return LeaseAcquisition(lease=_row_to_lease(created), created=True)


def heartbeat_lease(
    db: Database,
    task_id: str,
    owner_id: str,
    *,
    duration_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeaseRecord:
    """Renew a lease the caller owns, extending its expiry from *now*.

    Refuses if the caller is not the holder, and refuses if the lease already expired --
    a runner that let its lease lapse must go through reconciliation rather than quietly
    resurrecting ownership of work that something else may have started inspecting.
    """
    now = db.clock.now()
    with db.transaction() as con:
        row = con.execute(
            f"{_SELECT_LEASE} WHERE task_id = ? AND released_at IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            raise LeaseNotHeldError("no active lease to renew", task_id=task_id, owner_id=owner_id)
        lease = _row_to_lease(row)
        if lease.owner_id != owner_id:
            raise LeaseNotHeldError(
                "lease is held by a different owner",
                task_id=task_id,
                holder=lease.owner_id,
                requester=owner_id,
            )
        if not lease.is_live_at(now):
            raise LeaseExpiredError(
                "lease expired before renewal",
                task_id=task_id,
                owner_id=owner_id,
                expired_at=to_iso(lease.expires_at),
            )

        expires_at = now + timedelta(seconds=duration_seconds)
        con.execute(
            "UPDATE leases SET expires_at = ?, renewed_at = ? WHERE id = ?",
            (to_iso(expires_at), to_iso(now), lease.id),
        )
        refreshed = con.execute(f"{_SELECT_LEASE} WHERE id = ?", (lease.id,)).fetchone()
        return _row_to_lease(refreshed)


def release_lease(
    db: Database,
    task_id: str,
    owner_id: str,
    *,
    reason: str = "released",
) -> LeaseRecord | None:
    """Release a lease the caller owns.

    Idempotent: releasing when nothing is held returns ``None`` rather than raising, so a
    crash-restart cleanup path can call it unconditionally. Releasing *someone else's*
    lease is always an error.
    """
    now = db.clock.now()
    with db.transaction() as con:
        row = con.execute(
            f"{_SELECT_LEASE} WHERE task_id = ? AND released_at IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            return None
        lease = _row_to_lease(row)
        if lease.owner_id != owner_id:
            raise LeaseNotHeldError(
                "cannot release a lease held by a different owner",
                task_id=task_id,
                holder=lease.owner_id,
                requester=owner_id,
            )
        con.execute(
            "UPDATE leases SET released_at = ?, release_reason = ? WHERE id = ?",
            (to_iso(now), reason, lease.id),
        )
        refreshed = con.execute(f"{_SELECT_LEASE} WHERE id = ?", (lease.id,)).fetchone()
        return _row_to_lease(refreshed)


def expired_leases(db: Database) -> list[LeaseRecord]:
    """All unreleased leases whose expiry has passed -- the reconciliation work list."""
    now = to_iso(db.clock.now())
    rows = db.query(
        f"{_SELECT_LEASE} WHERE released_at IS NULL AND expires_at <= ? ORDER BY task_id",
        (now,),
    )
    return [_row_to_lease(row) for row in rows]


def reconcile_expired(
    db: Database,
    task_id: str,
    *,
    reason: str,
    reconciled_by: str,
) -> LeaseRecord:
    """Explicitly retire an expired lease after its side effects have been inspected.

    This is the *only* way an expired lease stops blocking a task, and it deliberately
    requires a human-or-supervisor-supplied ``reason``. Milestone 2 will make this the
    place where Git state is checked before a task is handed to a new runner; for now it
    records the decision so the audit trail shows who decided the takeover was safe and
    why.
    """
    if not reason:
        raise ValueError("reconciliation requires a reason")

    now = db.clock.now()
    with db.transaction() as con:
        row = con.execute(
            f"{_SELECT_LEASE} WHERE task_id = ? AND released_at IS NULL", (task_id,)
        ).fetchone()
        if row is None:
            raise LeaseNotHeldError("no unreleased lease to reconcile", task_id=task_id)
        lease = _row_to_lease(row)
        if lease.is_live_at(now):
            raise LeaseConflictError(
                task_id=task_id,
                holder=lease.owner_id,
                requester=reconciled_by,
                expires_at=to_iso(lease.expires_at),
            )
        con.execute(
            "UPDATE leases SET released_at = ?, release_reason = ? WHERE id = ?",
            (to_iso(now), f"reconciled by {reconciled_by}: {reason}", lease.id),
        )
        refreshed = con.execute(f"{_SELECT_LEASE} WHERE id = ?", (lease.id,)).fetchone()
        return _row_to_lease(refreshed)
