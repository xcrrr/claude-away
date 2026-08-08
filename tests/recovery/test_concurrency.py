"""Concurrency: two schedulers must never both own the same task.

These tests use *separate connections* -- and in one case separate OS processes -- against
a real file-backed database. A test that shares one connection would pass trivially and
prove nothing about the invariant that matters.

The races are made genuine with a barrier, so every contender is inside
:func:`acquire_lease` at the same moment rather than politely queueing.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import Database, open_database
from claude_away.core.evidence import (
    MAX_RUNS_PER_REQUIREMENT_PER_ATTEMPT,
    record_evidence,
)
from claude_away.core.leases import acquire_lease
from claude_away.core.models import EvidenceResult, EvidenceType
from claude_away.errors import LeaseConflictError, ValidationError
from tests.conftest import task_document

CONTENDERS = 8


@pytest.fixture
def populated(db_path: Path, db: Database) -> Path:
    repo.create_project(db, "api", path="/tmp/api")
    repo.create_goal(db, "ship", title="Ship", priority=90, success_criteria=["green"])
    repo.create_plan_version(db, reason="initial")
    repo.create_task(db, task_document("AWAY-0001"))
    db.close()
    return db_path


def test_single_active_lease_is_a_database_guarantee(populated: Path) -> None:
    """The deterministic version of the race tests below, and the stronger one.

    Thread and process races are probabilistic: a broken implementation can get lucky and
    serialise, as an unsafe deferred-transaction prototype did while this suite was being
    written. This test does not depend on timing at all. It writes a second active lease
    row *directly*, bypassing every application-level check, and asserts the database
    itself refuses -- which is what makes the invariant hold under any interleaving.
    """
    database = open_database(populated, migrate=False)
    try:
        acquire_lease(database, "AWAY-0001", "runner-a")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            database.connection.execute(
                "INSERT INTO leases(task_id, owner_id, acquired_at, expires_at, fence) "
                "VALUES ('AWAY-0001', 'runner-b', "
                "'2026-01-01T00:00:00.000000+00:00', "
                "'2026-01-01T01:00:00.000000+00:00', 99)"
            )

        # A *released* lease must not occupy the slot: history is retained, not blocking.
        database.connection.execute(
            "UPDATE leases SET released_at = '2026-01-01T00:30:00.000000+00:00' "
            "WHERE task_id = 'AWAY-0001'"
        )
        acquire_lease(database, "AWAY-0001", "runner-b")
        assert len(database.query("SELECT id FROM leases")) == 2
    finally:
        database.close()


def test_only_one_thread_acquires_the_lease(populated: Path) -> None:
    """The core invariant, raced across threads with independent connections."""
    barrier = threading.Barrier(CONTENDERS)
    winners: list[str] = []
    conflicts: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def contend(index: int) -> None:
        owner = f"runner-{index}"
        database = open_database(populated, migrate=False)
        try:
            barrier.wait(timeout=30)
            acquire_lease(database, "AWAY-0001", owner)
            with guard:
                winners.append(owner)
        except LeaseConflictError:
            with guard:
                conflicts.append(owner)
        except BaseException as exc:
            with guard:
                errors.append(exc)
        finally:
            database.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(CONTENDERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"unexpected errors: {errors}"
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(conflicts) == CONTENDERS - 1


def test_the_database_holds_exactly_one_active_lease(populated: Path) -> None:
    """Belt and braces: assert the stored state, not only the return values."""
    barrier = threading.Barrier(CONTENDERS)

    def contend(index: int) -> None:
        database = open_database(populated, migrate=False)
        try:
            barrier.wait(timeout=30)
            acquire_lease(database, "AWAY-0001", f"runner-{index}")
        except LeaseConflictError:
            pass
        finally:
            database.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(CONTENDERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    database = open_database(populated, migrate=False)
    try:
        rows = database.query(
            "SELECT owner_id FROM leases WHERE task_id = ? AND released_at IS NULL",
            ("AWAY-0001",),
        )
    finally:
        database.close()
    assert len(rows) == 1


_CHILD = """
import json, sys
from claude_away.core.db import open_database
from claude_away.core import state
from claude_away.core.evidence import (
    MAX_RUNS_PER_REQUIREMENT_PER_ATTEMPT,
    record_evidence,
)
from claude_away.core.leases import acquire_lease
from claude_away.core.models import EvidenceResult, EvidenceType
from claude_away.errors import LeaseConflictError, ValidationError, ReconciliationRequiredError

path, owner = sys.argv[1], sys.argv[2]
database = open_database(path, migrate=False)
try:
    acquire_lease(database, "AWAY-0001", owner)
    print(json.dumps({"owner": owner, "won": True}))
except (LeaseConflictError, ReconciliationRequiredError) as exc:
    print(json.dumps({"owner": owner, "won": False, "code": exc.code}))
finally:
    database.close()
"""


@pytest.mark.slow
def test_only_one_process_acquires_the_lease(populated: Path, tmp_path: Path) -> None:
    """Separate OS processes, separate SQLite connections, one winner.

    Threads share a process and could in principle be serialised by the GIL in a way that
    hides a race. Processes cannot be, so this is the version that actually exercises
    SQLite's file locking.
    """
    script = tmp_path / "contend.py"
    script.write_text(_CHILD, encoding="utf-8")

    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(populated), f"proc-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, f"child failed: {stderr}"
        results.append(json.loads(stdout.strip()))

    assert sum(1 for r in results if r["won"]) == 1, results


def test_concurrent_acquisition_of_different_tasks_all_succeed(db_path: Path, db: Database) -> None:
    """Sanity check in the other direction: the lock must not serialise unrelated work."""
    repo.create_project(db, "api", path="/tmp/api")
    repo.create_goal(db, "ship", title="Ship", priority=90, success_criteria=["green"])
    repo.create_plan_version(db, reason="initial")
    for index in range(1, CONTENDERS + 1):
        repo.create_task(db, task_document(f"AWAY-{index:04d}"))
    db.close()

    barrier = threading.Barrier(CONTENDERS)
    won: list[str] = []
    guard = threading.Lock()

    def contend(index: int) -> None:
        database = open_database(db_path, migrate=False)
        try:
            barrier.wait(timeout=30)
            acquire_lease(database, f"AWAY-{index:04d}", f"runner-{index}")
            with guard:
                won.append(f"AWAY-{index:04d}")
        finally:
            database.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(1, CONTENDERS + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(won) == CONTENDERS


def test_multiprocessing_start_method_is_available() -> None:
    """Guard against a platform where the process test would silently not run."""
    assert mp.get_start_method(allow_none=True) in (None, "fork", "spawn", "forkserver")


def test_concurrent_evidence_writes_respect_the_run_budget(populated: Path) -> None:
    """The budget check and the insert must be one transaction.

    Validating first and inserting afterwards lets every contender observe "budget not yet
    spent" before any of them writes -- which is exactly the re-run-until-green laundering
    the budget exists to prevent.
    """
    database = open_database(populated, migrate=False)
    try:
        state.refresh_readiness(database)
        acquire_lease(database, "AWAY-0001", "runner-a", duration_seconds=3600)
        state.start_attempt(database, "AWAY-0001", owner_id="runner-a")
        state.begin_verification(database, "AWAY-0001", owner_id="runner-a")
        attempt = state.active_attempt(database, "AWAY-0001")
        assert attempt is not None
        attempt_id = attempt.id
    finally:
        database.close()

    contenders = 6
    barrier = threading.Barrier(contenders)
    accepted: list[int] = []
    guard = threading.Lock()

    def contend(index: int) -> None:
        db = open_database(populated, migrate=False)
        try:
            barrier.wait(timeout=30)
            record_evidence(
                db,
                task_id="AWAY-0001",
                attempt_id=attempt_id,
                verification_id="unit-tests",
                type=EvidenceType.TEST,
                result=EvidenceResult.FAIL,
                summary=f"attempt {index}",
            )
            with guard:
                accepted.append(index)
        except ValidationError:
            pass
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    database = open_database(populated, migrate=False)
    try:
        rows = database.query(
            "SELECT id FROM evidence WHERE task_id = ? AND verification_id = 'unit-tests'",
            ("AWAY-0001",),
        )
    finally:
        database.close()
    assert len(rows) <= MAX_RUNS_PER_REQUIREMENT_PER_ATTEMPT
    assert len(accepted) == len(rows)


def test_concurrent_replay_of_one_idempotency_key_yields_one_effect(
    populated: Path,
) -> None:
    """A replayed key must return the original result, not a raw constraint error."""
    database = open_database(populated, migrate=False)
    try:
        state.refresh_readiness(database)
        acquire_lease(database, "AWAY-0001", "runner-a", duration_seconds=3600)
    finally:
        database.close()

    contenders = 6
    barrier = threading.Barrier(contenders)
    results: list[bool] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def contend(_index: int) -> None:
        db = open_database(populated, migrate=False)
        try:
            barrier.wait(timeout=30)
            outcome = state.start_attempt(
                db, "AWAY-0001", owner_id="runner-a", idempotency_key="shared-key"
            )
            with guard:
                results.append(outcome.replayed)
        except BaseException as exc:
            with guard:
                errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"unexpected errors: {errors}"
    assert results.count(False) == 1, "exactly one caller should do the real work"
    assert results.count(True) == contenders - 1

    database = open_database(populated, migrate=False)
    try:
        assert len(state.list_attempts(database, "AWAY-0001")) == 1
    finally:
        database.close()
