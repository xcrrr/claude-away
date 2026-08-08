"""Regressions for bypasses found by adversarial review of the state core.

Every test here corresponds to a hole that was **demonstrated working** against an earlier
revision of this package. They are the highest-value tests in the suite: each one is a
proven route to a `DONE` that nothing verified, or to a guard that could be stepped around.

If one of these ever fails, the central promise of the project is broken again.
"""

from __future__ import annotations

import sqlite3

import pytest

from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import Database
from claude_away.core.evidence import GateReason, evaluate_gate, record_evidence
from claude_away.core.leases import acquire_lease
from claude_away.core.models import (
    AttemptOutcome,
    EvidenceResult,
    EvidenceType,
    TaskStatus,
)
from claude_away.errors import (
    DatabaseError,
    InvalidStateError,
    StaleReplayError,
    TaskLeasedError,
    ValidationError,
)
from tests.conftest import load_task, task_document

OWNER = "runner-test"


def suspended_in_verifying(db: Database) -> None:
    """Drive a task to VERIFYING and then suspend its attempt.

    This is not a contrived state: it is the documented rate-limit path. STATE_MODEL says a
    rate-limit interruption "closes or suspends an attempt cleanly", and the task keeps its
    status. So a VERIFYING task with no active attempt is normal and supported -- which is
    exactly what made the bypass below reachable in ordinary operation rather than only
    under attack.
    """
    state.refresh_readiness(db)
    acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=99_999)
    state.start_attempt(db, "AWAY-0001", owner_id=OWNER)
    state.begin_verification(db, "AWAY-0001", owner_id=OWNER)
    state.suspend_attempt(
        db,
        "AWAY-0001",
        owner_id=OWNER,
        outcome=AttemptOutcome.RATE_LIMITED,
        reason="5h window exhausted",
    )
    assert state.active_attempt(db, "AWAY-0001") is None
    assert load_task(db, "AWAY-0001").status is TaskStatus.VERIFYING


class TestNullAttemptEvidence:
    """The critical one: evidence attributed to no attempt used to satisfy the gate.

    The gate matches ``attempt_id IS ?``. Evaluated with ``None`` -- which is what happens
    once an attempt is suspended -- it matched evidence rows whose ``attempt_id`` was NULL,
    and ``record_evidence`` defaulted that parameter to ``None``. A single unattributed
    "pass" therefore completed a task whose required check had never run.
    """

    def test_evidence_for_a_requirement_must_name_its_attempt(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        suspended_in_verifying(seeded)
        with pytest.raises(ValidationError, match="must name the attempt"):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                verification_id="unit-tests",
                type=EvidenceType.REVIEW,
                result=EvidenceResult.PASS,
                summary="All tests pass.",
            )

    def test_the_database_refuses_it_too(self, seeded: Database) -> None:
        """Second layer: a CHECK constraint, so no code path can write such a row."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        with pytest.raises(sqlite3.IntegrityError):
            seeded.connection.execute(
                "INSERT INTO evidence(task_id, attempt_id, verification_id,"
                " requirement_spec_hash, type, result, summary, created_at)"
                " VALUES ('AWAY-0001', NULL, 'unit-tests', 'deadbeef', 'review', 'pass',"
                " 'All tests pass.', '2026-01-01T00:00:00.000000+00:00')"
            )

    def test_the_gate_closes_when_there_is_no_active_attempt(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        suspended_in_verifying(seeded)
        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=None)
        assert not report.satisfied
        assert report.reason is GateReason.NO_ACTIVE_ATTEMPT

    def test_completion_requires_an_active_attempt(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        suspended_in_verifying(seeded)
        with pytest.raises(InvalidStateError, match="no active attempt"):
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.VERIFYING


class TestReplanCannotWeakenTheContract:
    """Creation refused an LLM-review-only contract; the replan path did not.

    ``update_verification_requirements`` deleted and reinserted the whole contract without
    re-running the invariant, so a replan could install exactly what ``create_task``
    rejects -- and then a model's own "looks good" was the only thing gating DONE.
    """

    @pytest.fixture
    def unleased_task(self, seeded: Database) -> Database:
        repo.create_task(seeded, task_document("AWAY-0001"))
        return seeded

    def test_review_only_contract_is_refused(self, unleased_task: Database) -> None:
        with pytest.raises(ValidationError, match="only thing gating"):
            repo.update_verification_requirements(
                unleased_task,
                "AWAY-0001",
                [{"id": "review", "type": "review", "required": True, "description": "lgtm?"}],
                plan_version=1,
            )

    def test_the_weakening_override_does_not_waive_it(self, unleased_task: Database) -> None:
        """`allow_weakening` exists to retire a *proven* check, not to remove the floor."""
        with pytest.raises(ValidationError, match="only thing gating"):
            repo.update_verification_requirements(
                unleased_task,
                "AWAY-0001",
                [{"id": "review", "type": "review", "required": True, "description": "lgtm?"}],
                plan_version=1,
                allow_weakening=True,
                justification="deliberate",
            )

    def test_all_optional_contract_is_refused(self, unleased_task: Database) -> None:
        with pytest.raises(ValidationError, match="no required verification"):
            repo.update_verification_requirements(
                unleased_task,
                "AWAY-0001",
                [{"id": "lint", "type": "command", "required": False, "command": "ruff"}],
                plan_version=1,
                allow_weakening=True,
                justification="deliberate",
            )


class TestReplaceCannotResurrectATask:
    """`INSERT OR REPLACE` used to walk straight past the absorbing-status guards.

    REPLACE resolves its conflict by deleting the old row, and SQLite does not fire DELETE
    triggers while doing so unless ``recursive_triggers`` is on. With it off, REPLACE
    dropped a CANCELLED task and reinserted it as PENDING -- past both
    ``tasks_are_never_deleted`` and ``tasks_cancelled_is_absorbing``.
    """

    def _replace(self, db: Database, task_id: str, status: str) -> None:
        db.connection.execute(
            "INSERT OR REPLACE INTO tasks(id, project_id, title, description, status,"
            " priority, risk, estimated_effort, human_required, created_by_plan_version,"
            " updated_by_plan_version, created_at, updated_at, status_changed_at)"
            f" VALUES ('{task_id}', 'api', 't', 'd', '{status}', 50, 'low', 'small', 0, 1, 1,"
            " '2026-01-01T00:00:00.000000+00:00', '2026-01-01T00:00:00.000000+00:00',"
            " '2026-01-01T00:00:00.000000+00:00')"
        )

    def test_cancelled_task_cannot_be_reopened_by_replace(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.cancel_task(seeded, "AWAY-0001", reason="obsolete")
        with pytest.raises(sqlite3.IntegrityError, match="never_deleted"):
            self._replace(seeded, "AWAY-0001", "PENDING")
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.CANCELLED

    def test_done_task_cannot_be_reopened_by_replace(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=99_999)
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
        state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        with pytest.raises(sqlite3.IntegrityError, match="never_deleted"):
            self._replace(seeded, "AWAY-0001", "PENDING")
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.DONE


class TestReplayMustStillDescribeReality:
    """A replay used to hand back a cheerful success for work that had since failed."""

    def test_replay_after_the_task_failed_is_refused(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=99_999)
        state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        state.fail_task(seeded, "AWAY-0001", reason="runner died")

        with pytest.raises(StaleReplayError) as caught:
            state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        assert caught.value.code == "stale_replay"
        assert caught.value.details["current_status"] == "FAILED"

    def test_a_genuine_replay_still_works(self, seeded: Database) -> None:
        """The fix must not break the case idempotency exists for."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=99_999)
        first = state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        second = state.start_attempt(seeded, "AWAY-0001", owner_id=OWNER, idempotency_key="k1")
        assert second.replayed
        assert second.attempt_id == first.attempt_id
        assert len(state.list_attempts(seeded, "AWAY-0001")) == 1


class TestTransitionEntryPointIsPrivate:
    def test_no_public_status_transition(self, seeded: Database) -> None:
        """It performs no validation of its own; reaching for it skips every guard.

        Renaming is not a security boundary -- an in-process caller can still use the
        private name -- but it stops the most plausible accident, which is a future
        contributor seeing a public-looking helper and assuming it is the supported route.
        """
        assert not hasattr(seeded, "status_transition")
        assert hasattr(seeded, "_status_transition")


class TestNestedRollbackIsReported:
    """A guard rollback inside a nested block used to surface as a raw sqlite3 error.

    `RAISE(ROLLBACK)` ends the transaction inside SQLite. If a caller swallowed that
    exception, the outer block's `COMMIT` failed with "cannot commit - no transaction is
    active" -- an unwrapped error that told nobody the real story, which is that every
    write in the block had been discarded.
    """

    def test_swallowed_guard_rollback_raises_a_domain_error(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)  # ensure the events table has a row to delete

        with (
            pytest.raises(DatabaseError, match="rolled back by a database guard"),
            seeded.transaction() as con,
        ):
            con.execute(
                "INSERT INTO projects(id, created_at) "
                "VALUES ('web', '2026-01-01T00:00:00.000000+00:00')"
            )
            try:
                with seeded.transaction() as inner:
                    inner.execute("DELETE FROM events")
            except sqlite3.IntegrityError:
                pass  # a caller that swallows the guard error

        # And the discarded write really is discarded, rather than half-committed.
        assert seeded.query_one("SELECT 1 FROM projects WHERE id = 'web'") is None


class TestContractFreezeIsCheckedUnderTheLock:
    def test_a_leased_task_still_refuses_a_contract_change(self, seeded: Database) -> None:
        """The freeze check now runs inside the transaction, not before it.

        Reading the lease first and writing afterwards is a time-of-check/time-of-use gap:
        a runner could take the lease in between and have its execution contract rewritten
        underneath it, which is exactly what STATE_MODEL forbids.
        """
        repo.create_task(seeded, task_document("AWAY-0001"))
        acquire_lease(seeded, "AWAY-0001", OWNER, duration_seconds=99_999)
        with pytest.raises(TaskLeasedError):
            repo.update_verification_requirements(
                seeded,
                "AWAY-0001",
                [
                    {"id": "unit-tests", "type": "command", "required": True, "command": "pytest"},
                    {"id": "extra", "type": "command", "required": True, "command": "mypy"},
                ],
                plan_version=1,
            )
        task = load_task(seeded, "AWAY-0001")
        assert {r.id for r in task.verification} == {"unit-tests"}
