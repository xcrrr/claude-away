"""The transition layer: legal edges, illegal edges, and the guards on both."""

from __future__ import annotations

import sqlite3

import pytest

from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import Database
from claude_away.core.evidence import record_evidence
from claude_away.core.leases import acquire_lease, release_lease
from claude_away.core.models import (
    AttemptOutcome,
    EvidenceResult,
    EvidenceType,
    TaskStatus,
)
from claude_away.errors import (
    IdempotencyConflictError,
    IllegalTransitionError,
    IntegrityViolationError,
    InvalidStateError,
    LeaseNotHeldError,
)
from tests.conftest import load_task, task_document

OWNER = "runner-test"


def drive_to_done(db: Database, task_id: str = "AWAY-0001") -> None:
    state.refresh_readiness(db)
    acquire_lease(db, task_id, OWNER)
    state.start_attempt(db, task_id, owner_id=OWNER)
    state.begin_verification(db, task_id, owner_id=OWNER)
    attempt = state.active_attempt(db, task_id)
    assert attempt is not None
    record_evidence(
        db,
        task_id=task_id,
        attempt_id=attempt.id,
        verification_id="unit-tests",
        type=EvidenceType.TEST,
        result=EvidenceResult.PASS,
        summary="ok",
    )
    state.mark_verified(db, task_id, owner_id=OWNER)


class TestHappyPath:
    def test_full_lifecycle(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.PENDING

        assert state.refresh_readiness(seeded) == ["AWAY-0001"]
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.READY

        acquire_lease(seeded, "AWAY-0001", OWNER)
        assert (
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER).to_status is TaskStatus.RUNNING
        )
        assert (
            state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER).to_status
            is TaskStatus.VERIFYING
        )

        attempt = state.active_attempt(seeded, "AWAY-0001")
        assert attempt is not None
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt.id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="ok",
        )
        assert state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER).to_status is TaskStatus.DONE

    def test_completion_closes_the_attempt(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        drive_to_done(seeded)
        attempts = state.list_attempts(seeded, "AWAY-0001")
        assert len(attempts) == 1
        assert attempts[0].outcome is AttemptOutcome.SUCCEEDED
        assert state.active_attempt(seeded, "AWAY-0001") is None

    def test_completion_event_records_the_gate_that_justified_it(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        drive_to_done(seeded)
        row = seeded.query_one(
            "SELECT payload FROM events WHERE kind = 'task_completed' AND task_id = ?",
            ("AWAY-0001",),
        )
        assert row is not None
        assert "unit-tests" in str(row["payload"])

    def test_readiness_cascades(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        repo.create_task(seeded, task_document("AWAY-0002", dependencies=["AWAY-0001"]))
        state.refresh_readiness(seeded)
        assert load_task(seeded, "AWAY-0002").status is TaskStatus.PENDING
        drive_to_done(seeded)
        assert state.refresh_readiness(seeded) == ["AWAY-0002"]

    def test_refresh_readiness_is_idempotent(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        assert state.refresh_readiness(seeded) == ["AWAY-0001"]
        assert state.refresh_readiness(seeded) == []


class TestIllegalTransitions:
    def test_done_cannot_be_cancelled(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        drive_to_done(seeded)
        with pytest.raises(IllegalTransitionError):
            state.cancel_task(seeded, "AWAY-0001", reason="changed my mind")

    def test_done_cannot_be_failed(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        drive_to_done(seeded)
        with pytest.raises(IllegalTransitionError):
            state.fail_task(seeded, "AWAY-0001", reason="regression")

    def test_done_is_absorbing_at_the_database_level_too(self, seeded: Database) -> None:
        """Even with the transition gate open, the database refuses to reopen DONE.

        Raw ``sqlite3.IntegrityError`` rather than the wrapped domain error, because this
        deliberately writes through the underlying connection: the point is that the guard
        holds one layer *below* anything the Python package does.
        """
        repo.create_task(seeded, task_document("AWAY-0001"))
        drive_to_done(seeded)
        with (
            pytest.raises(sqlite3.IntegrityError, match="done_is_absorbing"),
            seeded.status_transition() as con,
        ):
            con.execute("UPDATE tasks SET status = 'READY' WHERE id = 'AWAY-0001'")
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.DONE

    def test_cancelled_is_absorbing(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.cancel_task(seeded, "AWAY-0001", reason="not needed")
        with (
            pytest.raises(sqlite3.IntegrityError, match="cancelled_is_absorbing"),
            seeded.status_transition() as con,
        ):
            con.execute("UPDATE tasks SET status = 'PENDING' WHERE id = 'AWAY-0001'")
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.CANCELLED

    def test_pending_cannot_jump_to_running(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        acquire_lease(seeded, "AWAY-0001", OWNER)
        with pytest.raises(IllegalTransitionError):
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)

    def test_ready_cannot_verify_without_running(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        with pytest.raises(InvalidStateError):
            state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)

    def test_direct_status_write_is_refused(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        with pytest.raises(IntegrityViolationError, match="outside_transition_layer"):
            seeded.execute("UPDATE tasks SET status = 'DONE' WHERE id = 'AWAY-0001'")

    def test_task_cannot_be_born_done(self, seeded: Database) -> None:
        """INSERT OR REPLACE resolves as delete-then-insert, dodging UPDATE triggers."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        with pytest.raises(IntegrityViolationError, match="created_pending"):
            seeded.execute(
                "INSERT OR REPLACE INTO tasks(id, project_id, title, description, status,"
                " priority, risk, estimated_effort, human_required,"
                " created_by_plan_version, updated_by_plan_version, created_at,"
                " updated_at, status_changed_at)"
                " VALUES ('AWAY-0001','api','t','d','DONE',50,'low','small',0,1,1,"
                "'2026-01-01T00:00:00.000000+00:00','2026-01-01T00:00:00.000000+00:00',"
                "'2026-01-01T00:00:00.000000+00:00')"
            )
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.PENDING

    def test_tasks_cannot_be_deleted(self, seeded: Database) -> None:
        """Deletion would reopen the delete-then-reinsert route around every guard."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        with pytest.raises(IntegrityViolationError, match="never_deleted"):
            seeded.execute("DELETE FROM tasks WHERE id = 'AWAY-0001'")


class TestRetry:
    def test_retry_opens_a_new_attempt(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)
        first = state.active_attempt(seeded, "AWAY-0001")

        result = state.request_retry(seeded, "AWAY-0001", owner_id=OWNER, reason="tests failed")
        assert result.to_status is TaskStatus.RUNNING
        second = state.active_attempt(seeded, "AWAY-0001")
        assert second is not None and first is not None
        assert second.id != first.id
        assert second.attempt_number == first.attempt_number + 1

    def test_previous_attempt_becomes_immutable_history(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)
        first = state.active_attempt(seeded, "AWAY-0001")
        assert first is not None
        state.request_retry(seeded, "AWAY-0001", owner_id=OWNER, reason="nope")

        with pytest.raises(IntegrityViolationError, match="terminal"):
            seeded.execute(
                "UPDATE attempts SET failure_reason = 'rewritten' WHERE id = ?", (first.id,)
            )

    def test_retry_budget_exhaustion_fails_the_task(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, max_attempts=2)
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)
        state.request_retry(seeded, "AWAY-0001", owner_id=OWNER, reason="1", max_attempts=2)
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)
        result = state.request_retry(
            seeded, "AWAY-0001", owner_id=OWNER, reason="2", max_attempts=2
        )
        assert result.to_status is TaskStatus.FAILED

    def test_start_attempt_refuses_beyond_budget(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, max_attempts=1)
        state.mark_blocked(seeded, "AWAY-0001", reason="needs a human")
        state.resolve_blocker(seeded, "AWAY-0001", note="resolved")
        with pytest.raises(InvalidStateError, match="budget"):
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, max_attempts=1)


class TestBlocking:
    def test_block_and_resolve_returns_to_ready(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        state.mark_blocked(seeded, "AWAY-0001", reason="missing credential")
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.BLOCKED
        assert (
            state.resolve_blocker(seeded, "AWAY-0001", note="added").to_status is TaskStatus.READY
        )

    def test_resolution_falls_back_to_pending_when_dependencies_regressed(
        self, seeded: Database
    ) -> None:
        """The reason BLOCKED needs two exits: a dependency can die while we wait."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        repo.create_task(seeded, task_document("AWAY-0002", dependencies=["AWAY-0001"]))
        drive_to_done(seeded)
        state.refresh_readiness(seeded)

        acquire_lease(seeded, "AWAY-0002", OWNER)
        state.start_attempt(seeded, "AWAY-0002", owner_id=OWNER)
        state.mark_blocked(seeded, "AWAY-0002", reason="waiting")

        # A third task is cancelled and AWAY-0002 gains it as a dependency mid-block.
        repo.create_task(seeded, task_document("AWAY-0003"))
        state.cancel_task(seeded, "AWAY-0003", reason="obsolete")
        with seeded.transaction() as con:
            con.execute(
                "INSERT INTO task_dependencies(task_id, depends_on_id) VALUES (?, ?)",
                ("AWAY-0002", "AWAY-0003"),
            )
        assert state.resolve_blocker(seeded, "AWAY-0002", note="x").to_status is TaskStatus.PENDING

    def test_only_blocked_tasks_can_be_resolved(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        with pytest.raises(IllegalTransitionError):
            state.resolve_blocker(seeded, "AWAY-0001", note="x")

    def test_blocking_requires_a_reason(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        with pytest.raises(ValueError):
            state.mark_blocked(seeded, "AWAY-0001", reason="")


class TestLeaseEnforcement:
    def test_runner_transitions_require_a_lease(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        with pytest.raises(LeaseNotHeldError):
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)

    def test_a_different_owner_cannot_advance_the_task(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        with pytest.raises(LeaseNotHeldError):
            state.start_attempt(seeded, "AWAY-0001", owner_id="someone-else")

    def test_expired_lease_blocks_progress(self, seeded: Database, clock) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(LeaseNotHeldError, match="expired"):
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)

    def test_released_lease_blocks_progress(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        release_lease(seeded, "AWAY-0001", OWNER)
        with pytest.raises(LeaseNotHeldError):
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)


class TestSuspension:
    def test_suspend_closes_the_attempt_without_changing_status(self, seeded: Database) -> None:
        """A rate limit must never look like progress, and never like completion."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        attempt = state.suspend_attempt(
            seeded,
            "AWAY-0001",
            owner_id=OWNER,
            outcome=AttemptOutcome.RATE_LIMITED,
            reason="5h window exhausted",
        )
        assert attempt is not None
        assert attempt.outcome is AttemptOutcome.RATE_LIMITED
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.RUNNING
        assert state.active_attempt(seeded, "AWAY-0001") is None


class TestIdempotency:
    def test_replaying_start_attempt_returns_the_original(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        first = state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        second = state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        assert second.replayed
        assert second.attempt_id == first.attempt_id
        # Critically: no second attempt was created.
        assert len(state.list_attempts(seeded, "AWAY-0001")) == 1

    def test_replaying_completion_does_not_duplicate(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)
        attempt = state.active_attempt(seeded, "AWAY-0001")
        assert attempt is not None
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt.id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="ok",
        )
        first = state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="done-1")
        second = state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="done-1")
        assert second.replayed and second.event_id == first.event_id
        completions = seeded.query(
            "SELECT id FROM events WHERE kind = 'task_completed' AND task_id = ?",
            ("AWAY-0001",),
        )
        assert len(completions) == 1

    def test_reusing_a_key_with_different_content_is_rejected(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        repo.create_task(seeded, task_document("AWAY-0002"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        acquire_lease(seeded, "AWAY-0002", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="shared")
        with pytest.raises(IdempotencyConflictError):
            state.start_attempt(seeded, "AWAY-0002", owner_id=OWNER, idempotency_key="shared")


class TestCheckpoint:
    def test_checkpoint_merges_and_heartbeats(self, seeded: Database, clock) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=600)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        clock.advance(seconds=30)
        state.checkpoint_attempt(seeded, "AWAY-0001", owner_id=OWNER, checkpoint={"step": 1})
        clock.advance(seconds=30)
        attempt = state.checkpoint_attempt(
            seeded, "AWAY-0001", owner_id=OWNER, checkpoint={"files": 3}
        )
        assert attempt.checkpoint == {"step": 1, "files": 3}
        assert attempt.heartbeat_at is not None

    def test_checkpoint_requires_the_lease(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER)
        with pytest.raises(LeaseNotHeldError):
            state.checkpoint_attempt(seeded, "AWAY-0001", owner_id="other", checkpoint={})
