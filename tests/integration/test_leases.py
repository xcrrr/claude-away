"""Leases: single ownership, expiry semantics, and reconciliation."""

from __future__ import annotations

from datetime import timedelta

import pytest

from claude_away.clock import ManualClock
from claude_away.core import repository as repo
from claude_away.core.db import Database
from claude_away.core.leases import (
    acquire_lease,
    active_lease,
    expired_leases,
    heartbeat_lease,
    reconcile_expired,
    release_lease,
)
from claude_away.errors import (
    LeaseConflictError,
    LeaseExpiredError,
    LeaseNotHeldError,
    NotFoundError,
    ReconciliationRequiredError,
)
from tests.conftest import task_document

A = "runner-a"
B = "runner-b"


@pytest.fixture
def task(seeded: Database) -> Database:
    repo.create_task(seeded, task_document("AWAY-0001"))
    return seeded


class TestAcquisition:
    def test_acquire_creates_a_lease(self, task: Database) -> None:
        result = acquire_lease(task, "AWAY-0001", A)
        assert result.created
        assert result.lease.owner_id == A
        assert result.lease.fence == 1

    def test_same_owner_reacquiring_is_a_safe_replay(self, task: Database) -> None:
        """A runner restarting inside its own lease window must not be locked out."""
        first = acquire_lease(task, "AWAY-0001", A)
        second = acquire_lease(task, "AWAY-0001", A)
        assert not second.created
        assert second.lease.id == first.lease.id
        assert second.lease.fence == first.lease.fence

    def test_competing_owner_loses(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", A)
        with pytest.raises(LeaseConflictError) as caught:
            acquire_lease(task, "AWAY-0001", B)
        assert caught.value.holder == A
        assert caught.value.requester == B

    def test_acquire_after_release_gets_a_new_fence(self, task: Database) -> None:
        first = acquire_lease(task, "AWAY-0001", A)
        release_lease(task, "AWAY-0001", A)
        second = acquire_lease(task, "AWAY-0001", B)
        assert second.created
        # Monotonic fencing lets a resurrected runner be detected by comparison rather
        # than by trusting wall-clock expiry.
        assert second.lease.fence == first.lease.fence + 1

    def test_unknown_task_is_rejected(self, task: Database) -> None:
        with pytest.raises(NotFoundError):
            acquire_lease(task, "AWAY-9999", A)

    def test_zero_duration_is_rejected(self, task: Database) -> None:
        with pytest.raises(ValueError):
            acquire_lease(task, "AWAY-0001", A, duration_seconds=0)


class TestExpiry:
    def test_expired_lease_does_not_free_the_task(self, task: Database, clock: ManualClock) -> None:
        """The rule most systems get wrong: expiry is not permission to rerun.

        The previous runner may already have committed code or pushed a branch. Something
        has to look before a new runner takes over.
        """
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(ReconciliationRequiredError):
            acquire_lease(task, "AWAY-0001", B)

    def test_even_the_original_owner_must_reconcile(
        self, task: Database, clock: ManualClock
    ) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(ReconciliationRequiredError):
            acquire_lease(task, "AWAY-0001", A)

    def test_expired_lease_is_still_the_active_lease(
        self, task: Database, clock: ManualClock
    ) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        lease = active_lease(task, "AWAY-0001")
        assert lease is not None
        assert not lease.is_live_at(clock.now())

    def test_expired_leases_are_listed_for_reconciliation(
        self, task: Database, clock: ManualClock
    ) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        assert expired_leases(task) == []
        clock.advance(seconds=61)
        assert [lease.task_id for lease in expired_leases(task)] == ["AWAY-0001"]

    def test_reconciliation_frees_the_task(self, task: Database, clock: ManualClock) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        reconcile_expired(
            task, "AWAY-0001", reason="worktree inspected, no commits", reconciled_by=B
        )
        result = acquire_lease(task, "AWAY-0001", B)
        assert result.created

    def test_reconciliation_records_who_and_why(self, task: Database, clock: ManualClock) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        reconcile_expired(task, "AWAY-0001", reason="branch was clean", reconciled_by=B)
        row = task.query_one(
            "SELECT release_reason FROM leases WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            ("AWAY-0001",),
        )
        assert row is not None
        assert "branch was clean" in str(row["release_reason"])
        assert B in str(row["release_reason"])

    def test_a_live_lease_cannot_be_reconciled(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=600)
        with pytest.raises(LeaseConflictError):
            reconcile_expired(task, "AWAY-0001", reason="impatient", reconciled_by=B)

    def test_reconciliation_requires_a_reason(self, task: Database, clock: ManualClock) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(ValueError):
            reconcile_expired(task, "AWAY-0001", reason="", reconciled_by=B)


class TestHeartbeat:
    def test_renewal_extends_expiry(self, task: Database, clock: ManualClock) -> None:
        original = acquire_lease(task, "AWAY-0001", A, duration_seconds=100).lease
        clock.advance(seconds=50)
        renewed = heartbeat_lease(task, "AWAY-0001", A, duration_seconds=100)

        # Expiry is measured from *now*, not from the original acquisition.
        assert renewed.expires_at == clock.now() + timedelta(seconds=100)
        assert renewed.expires_at > original.expires_at
        assert renewed.renewed_at == clock.now()

        clock.advance(seconds=60)
        # Without the renewal this would already have expired at t=100.
        lease = active_lease(task, "AWAY-0001")
        assert lease is not None and lease.is_live_at(clock.now())

    def test_wrong_owner_cannot_renew(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", A)
        with pytest.raises(LeaseNotHeldError):
            heartbeat_lease(task, "AWAY-0001", B)

    def test_expired_lease_cannot_be_renewed(self, task: Database, clock: ManualClock) -> None:
        acquire_lease(task, "AWAY-0001", A, duration_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(LeaseExpiredError):
            heartbeat_lease(task, "AWAY-0001", A)

    def test_renewing_without_a_lease_fails(self, task: Database) -> None:
        with pytest.raises(LeaseNotHeldError):
            heartbeat_lease(task, "AWAY-0001", A)


class TestRelease:
    def test_release_is_idempotent(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", A)
        assert release_lease(task, "AWAY-0001", A) is not None
        # A crash-restart cleanup path calls this unconditionally; it must not explode.
        assert release_lease(task, "AWAY-0001", A) is None

    def test_wrong_owner_cannot_release(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", A)
        with pytest.raises(LeaseNotHeldError):
            release_lease(task, "AWAY-0001", B)
        assert active_lease(task, "AWAY-0001") is not None
