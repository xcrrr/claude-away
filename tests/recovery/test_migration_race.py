"""Concurrent first open of a fresh database must converge, not collide.

This file exists because `docs/V0_1_IMPLEMENTATION_PLAN.md` and commit 9242ec3 both
claimed this race was fixed while the fix was never actually in the code -- a `str.replace`
patch silently matched nothing, the commit message was written from intent rather than
from the diff, and no test covered the behaviour. A claim in a commit message is not
evidence; a test that fails against the old implementation is.

The rendezvous below fires *after* the first transaction of ``migrate()`` has committed,
which makes the test meaningful against both implementations without any sleep:

* **Broken** (check and apply in separate transactions): the first transaction is the
  version check. Both threads pass the barrier having each read version 0 with no lock
  held, then both enter the migration loop and the loser dies on ``CREATE TABLE meta``.
* **Fixed** (one transaction): the first transaction is the entire migration. The second
  thread cannot even ``BEGIN`` until the first commits, so it reads the new version and
  skips. Both reach the barrier already converged.

The assertion -- every opener must succeed -- does not depend on timing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

from claude_away.core.db import SCHEMA_VERSION, Database, open_database
from claude_away.errors import MigrationError

_RENDEZVOUS_TIMEOUT = 30


@contextmanager
def _rendezvous_after_first_commit(count: int) -> Iterator[threading.Barrier]:
    """Patch ``Database.transaction`` to sync openers after their first commit."""
    barrier = threading.Barrier(count, timeout=_RENDEZVOUS_TIMEOUT)
    original = Database.transaction

    @contextmanager
    def instrumented(self: Database, *args: Any, **kwargs: Any) -> Iterator[Any]:
        probing = getattr(self, "_probe", False) and not getattr(self, "_fired", False)
        with original(self, *args, **kwargs) as con:
            yield con
        # Deliberately outside the `with`: the transaction has committed and the write
        # lock is released, which is precisely the window the broken version left open.
        if probing:
            self._fired = True  # type: ignore[attr-defined]
            barrier.wait()

    Database.transaction = instrumented  # type: ignore[method-assign]
    try:
        yield barrier
    finally:
        Database.transaction = original  # type: ignore[method-assign]


def _open_concurrently(path: Path, openers: int) -> dict[str, tuple[str, Any]]:
    results: dict[str, tuple[str, Any]] = {}
    guard = threading.Lock()

    with _rendezvous_after_first_commit(openers) as barrier:

        def opener(name: str) -> None:
            db = Database(path)
            db._probe = True  # type: ignore[attr-defined]
            try:
                outcome: tuple[str, Any] = ("ok", db.migrate())
            except BaseException as exc:
                outcome = ("raised", f"{type(exc).__name__}: {exc}")
            finally:
                db._probe = False  # type: ignore[attr-defined]
                # Release a partner still waiting, so a failure reports as a failure
                # rather than hanging the suite.
                with suppress(BaseException):
                    barrier.wait(timeout=1)
                db.close()
            with guard:
                results[name] = outcome

        names = [f"opener-{index}" for index in range(openers)]
        threads = [threading.Thread(target=opener, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_RENDEZVOUS_TIMEOUT * 2)
        assert all(not t.is_alive() for t in threads), "an opener thread hung"

    return results


class TestConcurrentFirstOpen:
    def test_two_openers_converge(self, db_path: Path) -> None:
        """The regression. Fails on the pre-fix implementation with 'table meta already exists'."""
        results = _open_concurrently(db_path, openers=2)

        failed = {name: detail for name, (kind, detail) in results.items() if kind != "ok"}
        assert not failed, f"concurrent first open collided: {failed}"
        assert {detail for _kind, detail in results.values()} == {SCHEMA_VERSION}

    def test_many_openers_converge(self, db_path: Path) -> None:
        results = _open_concurrently(db_path, openers=5)
        failed = {name: detail for name, (kind, detail) in results.items() if kind != "ok"}
        assert not failed, f"concurrent first open collided: {failed}"

    def test_exactly_one_ledger_row_per_migration(self, db_path: Path) -> None:
        """Convergence must not mean 'applied twice and got away with it'."""
        _open_concurrently(db_path, openers=4)

        db = open_database(db_path, migrate=False)
        try:
            rows = db.query("SELECT version, name FROM schema_migrations ORDER BY version")
            versions = [int(row["version"]) for row in rows]
        finally:
            db.close()

        assert versions == sorted(set(versions)), "a migration was recorded more than once"
        assert versions == list(range(1, SCHEMA_VERSION + 1))

    def test_the_schema_is_intact_afterwards(self, db_path: Path) -> None:
        """A converged race must leave a fully usable database, not a half-built one."""
        _open_concurrently(db_path, openers=3)

        db = open_database(db_path, migrate=False)
        try:
            assert db.schema_version() == SCHEMA_VERSION
            assert db.missing_triggers() == []
            tables = {
                str(row["name"])
                for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            # Spot-check the tables the rest of the core depends on existing.
            assert {"tasks", "evidence", "attempts", "leases", "events"} <= tables
        finally:
            db.close()


class TestSequentialMigrationStillWorks:
    """The concurrency fix must not have broken the ordinary path."""

    def test_fresh_database(self, db_path: Path) -> None:
        db = open_database(db_path)
        try:
            assert db.schema_version() == SCHEMA_VERSION
        finally:
            db.close()

    def test_repeated_migrate_is_a_noop(self, db_path: Path) -> None:
        db = open_database(db_path)
        try:
            assert db.migrate() == SCHEMA_VERSION
            assert db.migrate() == SCHEMA_VERSION
            assert len(db.applied_migrations()) == SCHEMA_VERSION
        finally:
            db.close()

    def test_newer_schema_still_refused(self, db_path: Path) -> None:
        """The version guard must still fire from inside the single transaction."""
        from claude_away.errors import SchemaVersionError

        db = open_database(db_path)
        db.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (999, 'from the future', '2030-01-01T00:00:00.000000+00:00')"
        )
        db.close()

        with pytest.raises(SchemaVersionError) as caught:
            open_database(db_path)
        assert caught.value.details["found"] == 999


class TestLedgerFailureIsTyped:
    """The half of the migrate() fix that was claimed without a test behind it.

    `_ensure_migration_ledger()` was moved inside the error boundary so a `database is
    locked` during its DDL surfaces as a MigrationError rather than a bare
    sqlite3.OperationalError. Moving it back out left the suite green -- the same
    claim-without-evidence pattern the correction section exists to eliminate.
    """

    def test_a_locked_database_during_ledger_creation_is_a_migration_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.db"

        # Take an exclusive write lock from an unrelated connection and hold it. No sleeps:
        # the lock is held for the whole of the call under test.
        blocker = sqlite3.connect(path)
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            database = Database(path, busy_timeout_ms=50)
            with pytest.raises(MigrationError) as caught:
                database.migrate()
            assert "locked" in str(caught.value).lower()
            database.close()
        finally:
            blocker.rollback()
            blocker.close()

    def test_the_same_database_migrates_once_the_lock_is_released(self, tmp_path: Path) -> None:
        """The refusal is about contention, not about the database being broken."""
        path = tmp_path / "state.db"
        blocker = sqlite3.connect(path)
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute("BEGIN EXCLUSIVE")
        database = Database(path, busy_timeout_ms=50)
        with pytest.raises(MigrationError):
            database.migrate()
        blocker.rollback()
        blocker.close()

        assert database.migrate() == SCHEMA_VERSION
        database.close()
