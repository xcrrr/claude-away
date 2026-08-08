"""Durability: state must survive the process that wrote it.

Every test here closes the database and reopens it, because the question this milestone
has to answer is not "does the object hold the right value?" but "what would a supervisor
find on disk after being killed here?".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_away.clock import ManualClock
from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import SCHEMA_VERSION, Database, open_database
from claude_away.core.evidence import evaluate_gate, list_evidence, record_evidence
from claude_away.core.leases import acquire_lease, active_lease
from claude_away.core.models import EvidenceResult, EvidenceType, TaskStatus
from claude_away.errors import (
    IntegrityViolationError,
    MigrationError,
    SchemaVersionError,
)
from tests.conftest import load_task, task_document

OWNER = "runner-test"


def bootstrap(path: Path, clock: ManualClock) -> Database:
    db = open_database(path, clock=clock)
    repo.create_project(db, "api", path="/tmp/api")
    repo.create_goal(db, "ship", title="Ship", priority=90, success_criteria=["green"])
    repo.create_plan_version(db, reason="initial")
    return db


class TestMigrations:
    def test_fresh_database_is_migrated(self, db_path: Path, clock: ManualClock) -> None:
        db = open_database(db_path, clock=clock)
        try:
            assert db.schema_version() == SCHEMA_VERSION
            assert [m["version"] for m in db.applied_migrations()] == [1]
        finally:
            db.close()

    def test_migration_is_idempotent(self, db_path: Path, clock: ManualClock) -> None:
        db = open_database(db_path, clock=clock)
        try:
            assert db.migrate() == SCHEMA_VERSION
            assert db.migrate() == SCHEMA_VERSION
            assert len(db.applied_migrations()) == 1
        finally:
            db.close()

    def test_reopening_does_not_reapply(self, db_path: Path, clock: ManualClock) -> None:
        open_database(db_path, clock=clock).close()
        db = open_database(db_path, clock=clock)
        try:
            assert len(db.applied_migrations()) == 1
        finally:
            db.close()

    def test_newer_schema_is_refused(self, db_path: Path, clock: ManualClock) -> None:
        """An old build must never operate on a database it does not understand.

        Silently proceeding is how an unattended system corrupts a week of work.
        """
        db = open_database(db_path, clock=clock)
        db.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (999, 'from the future', '2030-01-01T00:00:00.000000+00:00')"
        )
        db.close()

        with pytest.raises(SchemaVersionError) as caught:
            open_database(db_path, clock=clock)
        assert caught.value.details["found"] == 999
        assert caught.value.details["supported"] == SCHEMA_VERSION

    def test_all_guard_triggers_exist(self, db_path: Path, clock: ManualClock) -> None:
        db = open_database(db_path, clock=clock)
        try:
            assert db.missing_triggers() == []
        finally:
            db.close()

    def test_missing_triggers_are_detected(self, db_path: Path, clock: ManualClock) -> None:
        """We cannot stop someone dropping a trigger; we can refuse to trust the file."""
        db = open_database(db_path, clock=clock)
        try:
            db.connection.execute("DROP TRIGGER evidence_no_update")
            assert db.missing_triggers() == ["evidence_no_update"]
        finally:
            db.close()

    def test_failed_migration_leaves_no_partial_schema(
        self, db_path: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash mid-migration must leave the previous version, not half a schema."""
        import claude_away.core.db as db_module

        broken = ((1, "broken", "CREATE TABLE good(a TEXT);\nCREATE TABLE bad(;\n"),)
        monkeypatch.setattr(db_module, "_MIGRATIONS", broken)

        database = Database(db_path, clock=clock)
        try:
            with pytest.raises(MigrationError):
                database.migrate()
            assert database.schema_version() == 0
            tables = {
                row["name"]
                for row in database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "good" not in tables
        finally:
            database.close()


class TestSurvivesReopen:
    def test_task_state_survives(self, db_path: Path, clock: ManualClock) -> None:
        db = bootstrap(db_path, clock)
        repo.create_task(db, task_document("AWAY-0001"))
        state.refresh_readiness(db)
        db.close()

        reopened = open_database(db_path, clock=clock)
        try:
            task = repo.get_task(reopened, "AWAY-0001")
            assert task is not None
            assert task.status is TaskStatus.READY
            assert task.verification[0].id == "unit-tests"
        finally:
            reopened.close()

    def test_evidence_and_gate_survive(self, db_path: Path, clock: ManualClock) -> None:
        db = bootstrap(db_path, clock)
        repo.create_task(db, task_document("AWAY-0001"))
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
        db.close()

        # A different process picks up exactly where the last one died.
        reopened = open_database(db_path, clock=clock)
        try:
            resumed = state.active_attempt(reopened, "AWAY-0001")
            assert resumed is not None and resumed.id == attempt.id
            assert len(list_evidence(reopened, "AWAY-0001")) == 1
            assert evaluate_gate(reopened, "AWAY-0001", attempt_id=resumed.id).satisfied
            assert (
                state.mark_verified(reopened, "AWAY-0001", owner_id=OWNER).to_status
                is TaskStatus.DONE
            )
        finally:
            reopened.close()

    def test_lease_survives_and_still_belongs_to_its_owner(
        self, db_path: Path, clock: ManualClock
    ) -> None:
        db = bootstrap(db_path, clock)
        repo.create_task(db, task_document("AWAY-0001"))
        acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=3600)
        db.close()

        reopened = open_database(db_path, clock=clock)
        try:
            lease = active_lease(reopened, "AWAY-0001")
            assert lease is not None
            assert lease.owner_id == OWNER
            assert lease.is_live_at(clock.now())
        finally:
            reopened.close()

    def test_runner_id_is_stable_across_reopen(self, db_path: Path, clock: ManualClock) -> None:
        db = bootstrap(db_path, clock)
        identity = repo.runner_id(db)
        db.close()

        reopened = open_database(db_path, clock=clock)
        try:
            assert repo.runner_id(reopened) == identity
        finally:
            reopened.close()

    def test_idempotency_records_survive(self, db_path: Path, clock: ManualClock) -> None:
        """The point of idempotency: it has to work across the crash, not within a run."""
        db = bootstrap(db_path, clock)
        repo.create_task(db, task_document("AWAY-0001"))
        state.refresh_readiness(db)
        acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=3600)
        first = state.start_attempt(db, "AWAY-0001", owner_id=OWNER, idempotency_key="boot-1")
        db.close()

        reopened = open_database(db_path, clock=clock)
        try:
            replay = state.start_attempt(
                reopened, "AWAY-0001", owner_id=OWNER, idempotency_key="boot-1"
            )
            assert replay.replayed
            assert replay.attempt_id == first.attempt_id
            assert len(state.list_attempts(reopened, "AWAY-0001")) == 1
        finally:
            reopened.close()


class TestTransactionAtomicity:
    def test_rollback_discards_the_whole_unit(self, db_path: Path, clock: ManualClock) -> None:
        db = bootstrap(db_path, clock)
        try:
            with pytest.raises(sqlite3.IntegrityError), db.transaction() as con:
                con.execute(
                    "INSERT INTO projects(id, created_at) VALUES ('web', ?)",
                    ("2026-01-01T00:00:00.000000+00:00",),
                )
                # Duplicate primary key: the whole transaction must be discarded.
                con.execute(
                    "INSERT INTO projects(id, created_at) VALUES ('web', ?)",
                    ("2026-01-01T00:00:00.000000+00:00",),
                )
            assert db.query_one("SELECT 1 FROM projects WHERE id = 'web'") is None
        finally:
            db.close()

    def test_failed_task_creation_leaves_nothing_behind(
        self, db_path: Path, clock: ManualClock
    ) -> None:
        """A task and its dependencies/criteria/requirements are one atomic unit."""
        db = bootstrap(db_path, clock)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                repo.create_task(db, task_document("AWAY-0001", dependencies=["AWAY-0404"]))
            assert repo.get_task(db, "AWAY-0001") is None
            assert db.query("SELECT * FROM verification_requirements") == []
            assert db.query("SELECT * FROM acceptance_criteria") == []
        finally:
            db.close()

    def test_connection_is_usable_after_a_guard_rollback(
        self, db_path: Path, clock: ManualClock
    ) -> None:
        """RAISE(ROLLBACK) ends the transaction inside SQLite; we must not double-rollback."""
        db = bootstrap(db_path, clock)
        try:
            repo.create_task(db, task_document("AWAY-0001"))
            with pytest.raises(IntegrityViolationError):
                db.execute("UPDATE tasks SET status = 'DONE' WHERE id = 'AWAY-0001'")
            # If the error had been masked, this would fail instead.
            assert load_task(db, "AWAY-0001").status is TaskStatus.PENDING
            repo.create_task(db, task_document("AWAY-0002"))
            assert repo.get_task(db, "AWAY-0002") is not None
        finally:
            db.close()
