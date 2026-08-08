"""The evidence gate: DONE is a claim backed by evidence.

If any test in this file starts passing for the wrong reason, the project's central
promise is broken. They are written to be adversarial rather than illustrative.
"""

from __future__ import annotations

import pytest

from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import Database
from claude_away.core.evidence import (
    MAX_RUNS_PER_REQUIREMENT_PER_ATTEMPT,
    GateReason,
    evaluate_gate,
    list_evidence,
    record_evidence,
    verification_spec_hash,
)
from claude_away.core.leases import acquire_lease
from claude_away.core.models import EvidenceResult, EvidenceType, TaskStatus, VerificationType
from claude_away.errors import (
    EvidenceIncompleteError,
    IntegrityViolationError,
    ValidationError,
)
from tests.conftest import load_task, task_document

OWNER = "runner-test"


def start(db: Database, task_id: str = "AWAY-0001") -> str:
    """Drive a task to VERIFYING and return the active attempt id."""
    state.refresh_readiness(db)
    acquire_lease(db, task_id, OWNER)
    state.start_attempt(db, task_id, owner_id=OWNER)
    state.begin_verification(db, task_id, owner_id=OWNER)
    attempt = state.active_attempt(db, task_id)
    assert attempt is not None
    return attempt.id


class TestGateBlocking:
    def test_done_impossible_without_evidence(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        start(seeded)
        with pytest.raises(EvidenceIncompleteError) as caught:
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        assert caught.value.missing == ["unit-tests"]
        assert load_task(seeded, "AWAY-0001").status is TaskStatus.VERIFYING

    @pytest.mark.parametrize(
        "result", [EvidenceResult.FAIL, EvidenceResult.ERROR, EvidenceResult.SKIPPED]
    )
    def test_done_impossible_with_non_passing_evidence(
        self, seeded: Database, result: EvidenceResult
    ) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=result,
            summary="did not pass",
        )
        with pytest.raises(EvidenceIncompleteError) as caught:
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        assert caught.value.failed == ["unit-tests"]

    def test_passing_evidence_opens_the_gate(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="42 passed",
        )
        result = state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        assert result.to_status is TaskStatus.DONE

    def test_evidence_for_a_different_requirement_does_not_satisfy(self, seeded: Database) -> None:
        repo.create_task(
            seeded,
            task_document(
                "AWAY-0001",
                verification=[
                    {"id": "unit-tests", "type": "command", "required": True, "command": "pytest"},
                    {"id": "typecheck", "type": "command", "required": True, "command": "mypy"},
                ],
            ),
        )
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="ok",
        )
        with pytest.raises(EvidenceIncompleteError) as caught:
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)
        assert caught.value.missing == ["typecheck"]


class TestStaleEvidence:
    def test_evidence_from_a_previous_attempt_does_not_satisfy(self, seeded: Database) -> None:
        """The headline staleness case: attempt 1's pass must not complete attempt 2."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        first_attempt = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=first_attempt,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="passed in attempt 1",
        )
        state.request_retry(seeded, "AWAY-0001", owner_id=OWNER, reason="reviewer asked")
        state.begin_verification(seeded, "AWAY-0001", owner_id=OWNER)

        second = state.active_attempt(seeded, "AWAY-0001")
        assert second is not None and second.id != first_attempt

        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=second.id)
        assert not report.satisfied
        assert report.missing == ("unit-tests",)
        with pytest.raises(EvidenceIncompleteError):
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)

    def test_editing_a_requirement_invalidates_its_evidence(self, seeded: Database) -> None:
        """A replan that changes what a check runs must not inherit the old pass."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="passed against the old command",
        )
        assert evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id).satisfied

        # Rewrite the requirement's command directly: the weakening guard is tested
        # separately, and here we want to isolate the spec-hash behaviour itself.
        new_hash = verification_spec_hash(
            type=VerificationType.COMMAND, command="pytest && mypy src"
        )
        with seeded.transaction() as con:
            con.execute(
                "UPDATE verification_requirements SET command = ?, spec_hash = ? "
                "WHERE task_id = ? AND verification_id = ?",
                ("pytest && mypy src", new_hash, "AWAY-0001", "unit-tests"),
            )

        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id)
        assert not report.satisfied
        # Reported as stale rather than merely missing: "a replan changed this" and
        # "nobody ran it" call for different operator responses.
        assert report.stale == ("unit-tests",)
        with pytest.raises(EvidenceIncompleteError):
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)

    def test_flipping_required_does_not_invalidate_evidence(self, seeded: Database) -> None:
        """optional -> required with an unchanged spec keeps the pass.

        The check genuinely ran and genuinely passed; only how much we care changed.
        Invalidating here would force a pointless re-run.
        """
        repo.create_task(
            seeded,
            task_document(
                "AWAY-0001",
                verification=[
                    {"id": "unit-tests", "type": "command", "required": True, "command": "pytest"},
                    {"id": "lint", "type": "command", "required": False, "command": "ruff check"},
                ],
            ),
        )
        attempt_id = start(seeded)
        for verification_id in ("unit-tests", "lint"):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id=verification_id,
                type=EvidenceType.COMMAND,
                result=EvidenceResult.PASS,
                summary="ok",
            )
        with seeded.transaction() as con:
            con.execute(
                "UPDATE verification_requirements SET required = 1 "
                "WHERE task_id = ? AND verification_id = 'lint'",
                ("AWAY-0001",),
            )
        assert evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id).satisfied


class TestOptionalRequirements:
    def test_optional_failure_does_not_block(self, seeded: Database) -> None:
        repo.create_task(
            seeded,
            task_document(
                "AWAY-0001",
                verification=[
                    {"id": "unit-tests", "type": "command", "required": True, "command": "pytest"},
                    {"id": "lint", "type": "command", "required": False, "command": "ruff check"},
                ],
            ),
        )
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.PASS,
            summary="ok",
        )
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="lint",
            type=EvidenceType.LINT,
            result=EvidenceResult.FAIL,
            summary="3 issues",
        )
        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id)
        assert report.satisfied
        # Never blocking, but never hidden either.
        assert report.optional_failed == ("lint",)
        assert state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER).to_status is TaskStatus.DONE

    def test_zero_required_requirements_closes_the_gate(self, seeded: Database) -> None:
        """A task where nothing is required must not be trivially completable.

        Task creation refuses this shape, so it is forced into the database directly to
        prove the gate itself is the independent second line of defence.
        """
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        with seeded.transaction() as con:
            con.execute(
                "UPDATE verification_requirements SET required = 0 WHERE task_id = ?",
                ("AWAY-0001",),
            )
        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id)
        assert not report.satisfied
        assert report.reason is GateReason.NO_REQUIRED_REQUIREMENTS
        assert report.required_total == 0
        with pytest.raises(EvidenceIncompleteError):
            state.mark_verified(seeded, "AWAY-0001", owner_id=OWNER)


class TestLatestWins:
    def test_a_later_pass_supersedes_an_earlier_failure(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        for result in (EvidenceResult.FAIL, EvidenceResult.PASS):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id="unit-tests",
                type=EvidenceType.TEST,
                result=result,
                summary=str(result.value),
            )
        assert evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id).satisfied
        # The failure stays in history: that record is what stops a later run pretending
        # verification was clean all along.
        assert [e.result for e in list_evidence(seeded, "AWAY-0001")] == [
            EvidenceResult.FAIL,
            EvidenceResult.PASS,
        ]

    def test_a_later_failure_supersedes_an_earlier_pass(self, seeded: Database) -> None:
        """ "A pass exists" would be a false-DONE generator; the latest result governs."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        for result in (EvidenceResult.PASS, EvidenceResult.FAIL):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id="unit-tests",
                type=EvidenceType.TEST,
                result=result,
                summary=str(result.value),
            )
        report = evaluate_gate(seeded, "AWAY-0001", attempt_id=attempt_id)
        assert not report.satisfied
        assert report.failed == ("unit-tests",)

    def test_rerun_budget_is_bounded(self, seeded: Database) -> None:
        """Otherwise a flaky check could simply be re-run until it happened to pass."""
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        for _ in range(MAX_RUNS_PER_REQUIREMENT_PER_ATTEMPT):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id="unit-tests",
                type=EvidenceType.TEST,
                result=EvidenceResult.FAIL,
                summary="flaky",
            )
        with pytest.raises(ValidationError, match="run budget"):
            record_evidence(
                seeded,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id="unit-tests",
                type=EvidenceType.TEST,
                result=EvidenceResult.PASS,
                summary="finally green",
            )


class TestAppendOnly:
    def test_evidence_cannot_be_updated(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.FAIL,
            summary="nope",
        )
        with pytest.raises(IntegrityViolationError, match="append_only"):
            seeded.execute("UPDATE evidence SET result = 'pass'")

    def test_evidence_cannot_be_deleted(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        attempt_id = start(seeded)
        record_evidence(
            seeded,
            task_id="AWAY-0001",
            attempt_id=attempt_id,
            verification_id="unit-tests",
            type=EvidenceType.TEST,
            result=EvidenceResult.FAIL,
            summary="nope",
        )
        with pytest.raises(IntegrityViolationError, match="append_only"):
            seeded.execute("DELETE FROM evidence")

    def test_events_are_append_only(self, seeded: Database) -> None:
        repo.create_task(seeded, task_document("AWAY-0001"))
        state.refresh_readiness(seeded)
        with pytest.raises(IntegrityViolationError, match="append_only"):
            seeded.execute("DELETE FROM events")


class TestSpecHash:
    def test_description_is_operative_for_manual_checks(self) -> None:
        """Changing what a human approves must invalidate the earlier approval."""
        first = verification_spec_hash(
            type=VerificationType.MANUAL, description="approve the API design"
        )
        second = verification_spec_hash(
            type=VerificationType.MANUAL, description="approve the production deploy"
        )
        assert first != second

    def test_description_is_commentary_for_command_checks(self) -> None:
        """Fixing a typo in a command's prose must not discard a passing test suite."""
        first = verification_spec_hash(
            type=VerificationType.COMMAND, command="pytest", description="runs tets"
        )
        second = verification_spec_hash(
            type=VerificationType.COMMAND, command="pytest", description="runs tests"
        )
        assert first == second

    def test_command_change_invalidates(self) -> None:
        assert verification_spec_hash(
            type=VerificationType.COMMAND, command="pytest"
        ) != verification_spec_hash(type=VerificationType.COMMAND, command="pytest -x")

    def test_hash_is_stable_across_calls(self) -> None:
        values = {
            verification_spec_hash(type=VerificationType.COMMAND, command="pytest")
            for _ in range(10)
        }
        assert len(values) == 1
