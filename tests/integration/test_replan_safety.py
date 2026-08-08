"""Replan safety: a plan change must not quietly lower the bar for DONE.

Two rules, both taken directly from the existing documents:

* STATE_MODEL: "replan never mutates a leased task's execution contract underneath it."
* CONTRIBUTING lists "silent weakening/removal of failed acceptance criteria" among the
  things the project will not merge -- so the controller refuses to *do* it either.
"""

from __future__ import annotations

import pytest

from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import Database
from claude_away.core.evidence import evaluate_gate, record_evidence
from claude_away.core.leases import acquire_lease, release_lease
from claude_away.core.models import EvidenceResult, EvidenceType
from claude_away.errors import TaskLeasedError, ValidationError
from tests.conftest import task_document

OWNER = "runner-test"

TWO_CHECKS = [
    {"id": "unit-tests", "type": "command", "required": True, "command": "pytest"},
    {"id": "typecheck", "type": "command", "required": True, "command": "mypy src"},
]


@pytest.fixture
def task(seeded: Database) -> Database:
    repo.create_task(seeded, task_document("AWAY-0001", verification=TWO_CHECKS))
    return seeded


def next_plan_version(db: Database) -> int:
    """Allocate a real plan version.

    ``tasks.updated_by_plan_version`` has a foreign key, so a replan cannot claim to come
    from a plan that was never recorded -- which is exactly what makes the replan diff
    reconstructable after a crash.
    """
    return repo.create_plan_version(db, reason="replan")


def fail_typecheck(db: Database) -> str:
    """Drive the task to VERIFYING with a failing typecheck. Returns the attempt id."""
    state.refresh_readiness(db)
    acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=3600)
    state.start_attempt(db, "AWAY-0001", owner_id=OWNER)
    state.begin_verification(db, "AWAY-0001", owner_id=OWNER)
    attempt = state.active_attempt(db, "AWAY-0001")
    assert attempt is not None
    record_evidence(
        db,
        task_id="AWAY-0001",
        attempt_id=attempt.id,
        verification_id="unit-tests",
        type=EvidenceType.TEST,
        result=EvidenceResult.PASS,
        summary="ok",
    )
    record_evidence(
        db,
        task_id="AWAY-0001",
        attempt_id=attempt.id,
        verification_id="typecheck",
        type=EvidenceType.TYPECHECK,
        result=EvidenceResult.FAIL,
        summary="3 errors",
    )
    return attempt.id


class TestLeaseFreeze:
    def test_a_leased_task_cannot_have_its_contract_rewritten(self, task: Database) -> None:
        acquire_lease(task, "AWAY-0001", OWNER, duration_seconds=3600)
        with pytest.raises(TaskLeasedError):
            repo.update_verification_requirements(task, "AWAY-0001", TWO_CHECKS, plan_version=1)

    def test_an_unleased_task_can_be_updated(self, task: Database) -> None:
        repo.update_verification_requirements(
            task,
            "AWAY-0001",
            [*TWO_CHECKS, {"id": "lint", "type": "command", "required": False, "command": "ruff"}],
            plan_version=1,
        )
        updated = repo.get_task(task, "AWAY-0001")
        assert updated is not None
        assert {r.id for r in updated.verification} == {"unit-tests", "typecheck", "lint"}


class TestWeakeningGuard:
    def test_dropping_a_failing_requirement_is_refused(self, task: Database) -> None:
        """The exploit this exists to stop: retire the failing check, declare success."""
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        with pytest.raises(ValidationError, match="weaken"):
            repo.update_verification_requirements(
                task, "AWAY-0001", [TWO_CHECKS[0]], plan_version=next_plan_version(task)
            )

    def test_demoting_a_failing_requirement_is_refused(self, task: Database) -> None:
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        demoted = [TWO_CHECKS[0], {**TWO_CHECKS[1], "required": False}]
        with pytest.raises(ValidationError, match="weaken"):
            repo.update_verification_requirements(
                task, "AWAY-0001", demoted, plan_version=next_plan_version(task)
            )

    def test_rewriting_a_failing_requirement_is_refused(self, task: Database) -> None:
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        softened = [TWO_CHECKS[0], {**TWO_CHECKS[1], "command": "true"}]
        with pytest.raises(ValidationError, match="weaken"):
            repo.update_verification_requirements(
                task, "AWAY-0001", softened, plan_version=next_plan_version(task)
            )

    def test_dropping_a_passing_requirement_is_allowed(self, task: Database) -> None:
        """Retiring work that genuinely passed is legitimate replanning, not weakening."""
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        repo.update_verification_requirements(
            task, "AWAY-0001", [TWO_CHECKS[1]], plan_version=next_plan_version(task)
        )
        updated = repo.get_task(task, "AWAY-0001")
        assert updated is not None
        assert {r.id for r in updated.verification} == {"typecheck"}

    def test_tightening_is_always_allowed(self, task: Database) -> None:
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        repo.update_verification_requirements(
            task,
            "AWAY-0001",
            [
                *TWO_CHECKS,
                {"id": "security", "type": "command", "required": True, "command": "bandit"},
            ],
            plan_version=next_plan_version(task),
        )
        updated = repo.get_task(task, "AWAY-0001")
        assert updated is not None
        assert len(updated.verification) == 3

    def test_override_requires_a_justification(self, task: Database) -> None:
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        with pytest.raises(ValidationError, match="justification"):
            repo.update_verification_requirements(
                task,
                "AWAY-0001",
                [TWO_CHECKS[0]],
                plan_version=next_plan_version(task),
                allow_weakening=True,
            )

    def test_deliberate_override_is_recorded(self, task: Database) -> None:
        """A human may override, but the decision goes in the ledger permanently."""
        fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        repo.update_verification_requirements(
            task,
            "AWAY-0001",
            [TWO_CHECKS[0]],
            plan_version=next_plan_version(task),
            allow_weakening=True,
            justification="typecheck is broken upstream, tracked in #42",
        )
        row = task.query_one(
            "SELECT payload FROM events WHERE kind = 'verification_contract_updated'"
        )
        assert row is not None
        assert "#42" in str(row["payload"])

    def test_weakening_does_not_open_the_gate_by_accident(self, task: Database) -> None:
        """Even after a legitimate override, the remaining checks still have to pass."""
        attempt_id = fail_typecheck(task)
        release_lease(task, "AWAY-0001", OWNER)
        repo.update_verification_requirements(
            task,
            "AWAY-0001",
            [TWO_CHECKS[0], {**TWO_CHECKS[1], "command": "mypy --strict src"}],
            plan_version=next_plan_version(task),
            allow_weakening=True,
            justification="switching to strict mode",
        )
        report = evaluate_gate(task, "AWAY-0001", attempt_id=attempt_id)
        assert not report.satisfied
        # The rewritten check has no evidence under its new definition.
        assert "typecheck" in report.missing
