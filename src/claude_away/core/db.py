"""SQLite storage: connection policy, transaction discipline and versioned migrations.

SQLite is the authoritative transactional state for Claude Away. Everything in this
module exists to make that authority hold up under a process that may be killed at any
moment and restarted by a different runner.

Three decisions deserve explanation, because getting them wrong is subtle:

**WAL, and what it does not do.** We enable WAL because it lets readers proceed while a
writer is active, which matters for ``awayctl status`` running against a live supervisor.
WAL does *not* give us multiple concurrent writers -- SQLite still serialises writes with
a single write lock. So WAL is a read-availability improvement, not a concurrency
solution, and the lease design does not lean on it.

**BEGIN IMMEDIATE for anything that writes.** SQLite's default deferred transaction takes
a read lock first and only tries to upgrade to a write lock on the first write. If two
connections both read and then try to upgrade, one gets ``SQLITE_BUSY`` *and cannot wait
for it* -- ``busy_timeout`` does not help an upgrade deadlock, because backing off would
mean the reader's snapshot is already stale. Any read-then-write sequence (which is
exactly what "check the lease, then take it" is) must therefore start with
``BEGIN IMMEDIATE`` so the write lock is taken up front. This is the single most
important line in the file.

**A status column that only one code path may change.** ``tasks.status`` is protected by
a trigger that calls an application-defined function registered on our connection. The
transition layer briefly opens that gate; nothing else can. A consequence worth stating:
a connection that has *not* registered the function -- the ``sqlite3`` CLI, an ad-hoc
script, a future contributor's debugging session -- cannot change a task status at all,
because the trigger fails with "no such function". Reads are entirely unaffected.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from claude_away.clock import Clock, SystemClock, to_iso
from claude_away.errors import (
    IntegrityViolationError,
    MigrationError,
    SchemaVersionError,
)

__all__ = [
    "EXPECTED_TRIGGERS",
    "SCHEMA_VERSION",
    "TRANSITION_GUARD_FUNCTION",
    "Database",
]

SCHEMA_VERSION = 1
"""The schema version this build creates and can operate on."""

TRANSITION_GUARD_FUNCTION = "claude_away_transition_ok"
"""Name of the connection-scoped function guarding ``tasks.status`` updates."""

_DEFAULT_BUSY_TIMEOUT_MS = 5_000

EXPECTED_TRIGGERS: frozenset[str] = frozenset(
    {
        "evidence_no_update",
        "evidence_no_delete",
        "events_no_update",
        "events_no_delete",
        "attempts_terminal_immutable",
        "attempts_identity_immutable",
        "tasks_done_is_absorbing",
        "tasks_cancelled_is_absorbing",
        "tasks_status_requires_transition_layer",
        "tasks_are_born_pending",
        "tasks_are_never_deleted",
    }
)
"""The guard triggers that must exist for the state database to be trustworthy.

Anyone with write access to the file can ``DROP TRIGGER``. SQLite offers no way to prevent
that, and pretending otherwise would be worse than admitting it. What we *can* do is
detect it: :func:`Database.missing_triggers` reports any that have gone missing, and
``awayctl doctor`` refuses to call the state healthy when they have. Detection, not
prevention.
"""


# ======================================================================================
# Migrations
# ======================================================================================
#
# Migrations are an append-only list. Never edit a released migration in place -- add a
# new one. Each runs inside the same transaction that records it, so a crash mid-migration
# leaves the database at the previous version rather than half-upgraded.

_MIGRATION_0001 = """
-- ---------------------------------------------------------------- controller identity
-- The runner id is generated once, here, rather than read from configuration. A config
-- file gets copied between machines; two runners sharing an owner id would silently steal
-- each other's leases, which is the precise failure leases exist to prevent. Binding
-- identity to the state database instead makes that impossible to misconfigure.
CREATE TABLE meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------ enrolled projects
CREATE TABLE projects (
    id             TEXT PRIMARY KEY,
    path           TEXT,
    repository     TEXT,
    default_branch TEXT,
    created_at     TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------- user-stated goals
CREATE TABLE goals (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    priority   INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 100),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE goal_success_criteria (
    goal_id  TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (goal_id, position)
) STRICT;

-- --------------------------------------------------------------------- plan versioning
-- Monotonic. Every task records the plan version that created it and the latest version
-- that modified its scheduling metadata, so a replan diff can always be reconstructed.
CREATE TABLE plan_versions (
    version    INTEGER PRIMARY KEY CHECK (version >= 1),
    created_at TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '{}'
) STRICT;

-- ------------------------------------------------------------------------------- tasks
CREATE TABLE tasks (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    title                   TEXT NOT NULL,
    description             TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN (
                                'PENDING','READY','RUNNING','VERIFYING',
                                'DONE','BLOCKED','FAILED','CANCELLED')),
    priority                INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 100),
    risk                    TEXT NOT NULL CHECK (risk IN ('low','medium','high')),
    estimated_effort        TEXT NOT NULL CHECK (estimated_effort IN
                                ('tiny','small','medium','large')),
    human_required          INTEGER NOT NULL CHECK (human_required IN (0,1)),
    created_by_plan_version INTEGER NOT NULL REFERENCES plan_versions(version),
    updated_by_plan_version INTEGER NOT NULL REFERENCES plan_versions(version),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    status_changed_at       TEXT NOT NULL,
    CHECK (updated_by_plan_version >= created_by_plan_version)
) STRICT;

CREATE INDEX tasks_status_idx     ON tasks(status);
CREATE INDEX tasks_project_idx    ON tasks(project_id);

CREATE TABLE task_goals (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE RESTRICT,
    PRIMARY KEY (task_id, goal_id)
) STRICT;

-- Self-dependency is rejected by a CHECK constraint as well as by the DAG validator.
-- Duplicate dependencies are impossible by primary key. Neither is left to convention.
CREATE TABLE task_dependencies (
    task_id           TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id <> depends_on_id)
) STRICT;

CREATE INDEX task_dependencies_depends_on_idx ON task_dependencies(depends_on_id);

-- Criteria carry stable ids for the same reason verification requirements do: PRODUCT_SPEC
-- forbids a replan from silently weakening acceptance criteria, and detecting that requires
-- identity that survives reordering.
CREATE TABLE acceptance_criteria (
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    position     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (task_id, criterion_id)
) STRICT;

CREATE TABLE expected_artifacts (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    path     TEXT NOT NULL,
    PRIMARY KEY (task_id, position)
) STRICT;

-- ------------------------------------------------------- declared verification contract
-- `verification_id` is stable within a task and is what evidence points at. `spec_hash`
-- is a content hash of the requirement's meaningful fields: when a replan edits a
-- requirement the hash changes, which invalidates evidence gathered under the old
-- definition instead of letting it silently satisfy the new one.
CREATE TABLE verification_requirements (
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    verification_id TEXT NOT NULL,
    position        INTEGER NOT NULL,
    type            TEXT NOT NULL CHECK (type IN
                        ('command','artifact','git','review','manual')),
    required        INTEGER NOT NULL CHECK (required IN (0,1)),
    command         TEXT,
    path            TEXT,
    description     TEXT,
    spec_hash       TEXT NOT NULL,
    PRIMARY KEY (task_id, verification_id),
    -- The conditional shape is enforced in the database as well as in JSON Schema, so a
    -- structurally meaningless requirement cannot be persisted by any code path.
    CHECK (type <> 'command'  OR (command IS NOT NULL AND length(command) > 0)),
    CHECK (type <> 'artifact' OR (path    IS NOT NULL AND length(path)    > 0))
) STRICT;

-- ---------------------------------------------------------------------------- attempts
CREATE TABLE attempts (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    runner_id      TEXT NOT NULL,
    mode           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    outcome        TEXT CHECK (outcome IS NULL OR outcome IN
                        ('succeeded','failed','abandoned','interrupted','rate_limited')),
    failure_reason TEXT,
    base_commit    TEXT,
    branch         TEXT,
    worktree_path  TEXT,
    session_id     TEXT,
    workflow_id    TEXT,
    checkpoint     TEXT NOT NULL DEFAULT '{}',
    heartbeat_at   TEXT,
    UNIQUE (task_id, attempt_number),
    -- An attempt is terminal exactly when it has an outcome; the two fields cannot drift.
    CHECK ((outcome IS NULL) = (finished_at IS NULL))
) STRICT;

CREATE INDEX attempts_task_idx ON attempts(task_id);

-- At most one active attempt per task. Combined with the lease this makes "two runners
-- both think they own this task" structurally impossible rather than merely unlikely.
CREATE UNIQUE INDEX attempts_one_active_per_task
    ON attempts(task_id) WHERE outcome IS NULL;

-- ---------------------------------------------------------------------------- evidence
-- Append-only. Enforced by triggers below, not by convention.
CREATE TABLE evidence (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    attempt_id             TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
    verification_id        TEXT,
    requirement_spec_hash  TEXT,
    type                   TEXT NOT NULL CHECK (type IN (
                               'command','test','lint','typecheck','build','artifact',
                               'git','review','pull_request','manual_approval')),
    result                 TEXT NOT NULL CHECK (result IN
                               ('pass','fail','error','skipped')),
    summary                TEXT NOT NULL,
    metadata               TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    -- Frozen at write time and deliberately NOT read by the gate, which always consults
    -- the requirement's *current* `required` flag. It exists so an audit can answer "was
    -- this check mandatory when it ran?" -- otherwise flipping a requirement to optional
    -- would retroactively rewrite what the history appears to say.
    required_at_run        INTEGER CHECK (required_at_run IS NULL
                                          OR required_at_run IN (0,1)),
    -- Evidence aimed at a specific requirement must carry the hash of the requirement
    -- definition it was produced against; otherwise the gate cannot detect staleness.
    CHECK (verification_id IS NULL OR requirement_spec_hash IS NOT NULL)
) STRICT;

CREATE INDEX evidence_task_idx    ON evidence(task_id);
CREATE INDEX evidence_attempt_idx ON evidence(attempt_id);
CREATE INDEX evidence_gate_idx    ON evidence(task_id, attempt_id, verification_id, result);

-- ------------------------------------------------------------------------------ leases
-- Exactly one *unreleased* lease per task, enforced by a partial unique index.
--
-- Note what this deliberately does NOT do: an expired-but-unreleased lease still occupies
-- the slot. Expiry alone does not hand the task to a new runner, because the previous
-- runner may have committed work or started an external action before dying. Taking over
-- requires an explicit reconciliation that releases the old lease and records why.
CREATE TABLE leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    owner_id    TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    renewed_at  TEXT,
    released_at TEXT,
    release_reason TEXT,
    -- Monotonic per task. A resurrected process holding an old lease object can be
    -- detected by comparing fences, which matters once real execution exists.
    fence       INTEGER NOT NULL CHECK (fence >= 1)
) STRICT;

CREATE UNIQUE INDEX leases_one_active_per_task
    ON leases(task_id) WHERE released_at IS NULL;

CREATE UNIQUE INDEX leases_fence_unique ON leases(task_id, fence);

-- ----------------------------------------------------------------- audit event ledger
-- Append-only record of everything that changed state. This is what a return briefing
-- and a post-incident investigation are reconstructed from.
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    task_id         TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    attempt_id      TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
    from_status     TEXT,
    to_status       TEXT,
    actor           TEXT NOT NULL DEFAULT 'system',
    payload         TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE INDEX events_task_idx ON events(task_id, id);
CREATE INDEX events_kind_idx ON events(kind, id);

-- ------------------------------------------------------------------------ idempotency
-- Replaying an identical operation after a crash returns the recorded result. Reusing a
-- key with different content is a caller bug and is rejected rather than guessed at.
CREATE TABLE idempotency_keys (
    key          TEXT PRIMARY KEY,
    operation    TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
) STRICT;

-- =====================================================================================
-- Immutability and transition triggers
-- =====================================================================================

-- Evidence is history. It is never edited and never deleted.
CREATE TRIGGER evidence_no_update
BEFORE UPDATE ON evidence
BEGIN
    SELECT RAISE(ROLLBACK, 'evidence_is_append_only');
END;

CREATE TRIGGER evidence_no_delete
BEFORE DELETE ON evidence
BEGIN
    SELECT RAISE(ROLLBACK, 'evidence_is_append_only');
END;

-- The audit ledger is history too.
CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ROLLBACK, 'events_are_append_only');
END;

CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ROLLBACK, 'events_are_append_only');
END;

-- A finished attempt is immutable history. While an attempt is active, only an explicitly
-- bounded set of recovery fields may change (heartbeat and checkpoint); identity, timing
-- and provenance may not. This keeps "attempts are immutable" true in the way that
-- matters without making crash recovery impossible.
CREATE TRIGGER attempts_terminal_immutable
BEFORE UPDATE ON attempts
FOR EACH ROW WHEN OLD.outcome IS NOT NULL
BEGIN
    SELECT RAISE(ROLLBACK, 'attempt_is_terminal');
END;

CREATE TRIGGER attempts_identity_immutable
BEFORE UPDATE ON attempts
FOR EACH ROW WHEN
       NEW.id             <> OLD.id
    OR NEW.task_id        <> OLD.task_id
    OR NEW.attempt_number <> OLD.attempt_number
    OR NEW.runner_id      <> OLD.runner_id
    OR NEW.started_at     <> OLD.started_at
BEGIN
    SELECT RAISE(ROLLBACK, 'attempt_identity_is_immutable');
END;

-- DONE is absorbing. This is enforced in the database itself, below the transition layer,
-- so that even a bug in that layer cannot silently reopen completed work.
CREATE TRIGGER tasks_done_is_absorbing
BEFORE UPDATE OF status ON tasks
FOR EACH ROW WHEN OLD.status = 'DONE' AND NEW.status <> 'DONE'
BEGIN
    SELECT RAISE(ROLLBACK, 'done_is_absorbing');
END;

-- CANCELLED is absorbing for the same reason: a retired task is retired.
CREATE TRIGGER tasks_cancelled_is_absorbing
BEFORE UPDATE OF status ON tasks
FOR EACH ROW WHEN OLD.status = 'CANCELLED' AND NEW.status <> 'CANCELLED'
BEGIN
    SELECT RAISE(ROLLBACK, 'cancelled_is_absorbing');
END;

-- Every status change must go through the transition layer, which briefly opens this
-- gate. A connection that has not registered the guard function cannot change a status at
-- all -- the trigger fails with "no such function" -- which is exactly the protection we
-- want against ad-hoc writes from outside the controller.
CREATE TRIGGER tasks_status_requires_transition_layer
BEFORE UPDATE OF status ON tasks
FOR EACH ROW WHEN NEW.status <> OLD.status
    AND claude_away_transition_ok() <> 1
BEGIN
    SELECT RAISE(ROLLBACK, 'status_change_outside_transition_layer');
END;

-- A task is born PENDING and reaches every other state through a recorded transition.
--
-- Without this, `INSERT INTO tasks(... 'DONE' ...)` creates a completed task that never
-- passed the evidence gate, and `INSERT OR REPLACE` does the same to an existing row --
-- REPLACE resolves as delete-then-insert, so UPDATE triggers never fire. Both were
-- verified to bypass the status guard before this trigger existed.
CREATE TRIGGER tasks_are_born_pending
BEFORE INSERT ON tasks
FOR EACH ROW WHEN NEW.status <> 'PENDING'
BEGIN
    SELECT RAISE(ROLLBACK, 'tasks_must_be_created_pending');
END;

-- Tasks are retired by CANCELLED, never deleted. Deletion would otherwise reopen the
-- delete-then-reinsert route around every status guard, and would silently orphan the
-- evidence and attempts that justify what already happened.
CREATE TRIGGER tasks_are_never_deleted
BEFORE DELETE ON tasks
BEGIN
    SELECT RAISE(ROLLBACK, 'tasks_are_never_deleted');
END;
"""


_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "initial deterministic state core", _MIGRATION_0001),
)


# ======================================================================================
# Database
# ======================================================================================


class Database:
    """A connection to the Claude Away state database.

    Single-threaded by construction, not merely by convention: ``sqlite3`` defaults to
    ``check_same_thread=True``, so using one of these from another thread raises
    ``ProgrammingError``. Each thread or process opens its own :class:`Database`. That is
    the model the concurrency tests exercise, and it is the model a future supervisor will
    use -- coordination between them happens through leases in the database, never through
    shared objects in memory.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock | None = None,
        busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.clock: Clock = clock or SystemClock()
        self._busy_timeout_ms = busy_timeout_ms
        self._transition_gate_open = False
        self._depth = 0
        self._closed = False

        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(self.path),
            # We manage transactions explicitly; Python's implicit handling would insert
            # its own BEGIN at times we do not control, which is incompatible with the
            # BEGIN IMMEDIATE discipline the lease logic depends on.
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()

    # ---------------------------------------------------------------- connection setup

    def _configure(self) -> None:
        con = self._connection
        con.create_function(
            TRANSITION_GUARD_FUNCTION,
            0,
            self._transition_guard,
            deterministic=False,
        )
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
        # NORMAL is the right durability point under WAL: it survives process crashes
        # (our actual threat model) and only risks the very last commit on host power
        # loss, in exchange for a large write-throughput gain.
        con.execute("PRAGMA synchronous = NORMAL")
        if str(self.path) != ":memory:":
            con.execute("PRAGMA journal_mode = WAL")

    def _transition_guard(self) -> int:
        return 1 if self._transition_gate_open else 0

    # ------------------------------------------------------------------- transactions

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a block inside a transaction.

        ``immediate=True`` (the default) issues ``BEGIN IMMEDIATE``, taking the write lock
        up front. Every read-then-write sequence must use it -- see the module docstring
        for why a deferred transaction cannot safely upgrade.

        Nesting is supported and collapses into the outermost transaction, so composing
        two atomic operations does not accidentally commit halfway through.
        """
        if self._closed:
            raise ValueError("database is closed")

        if self._depth > 0:
            self._depth += 1
            try:
                yield self._connection
            finally:
                self._depth -= 1
            return

        con = self._connection
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._depth = 1
        try:
            yield con
        except BaseException:
            self._depth = 0
            # The immutability and transition guards raise RAISE(ROLLBACK), which ends the
            # transaction inside SQLite before the exception reaches us. Issuing another
            # ROLLBACK would fail with "cannot rollback - no transaction is active" and
            # mask the real error, so check first.
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            con.execute("COMMIT")

    @contextmanager
    def _open_transition_gate(self) -> Iterator[None]:
        """Briefly permit ``tasks.status`` updates on this connection.

        No lock is needed and none would help. ``sqlite3`` defaults to
        ``check_same_thread=True``, so a :class:`Database` raises ``ProgrammingError`` if
        touched from any thread but the one that created it -- the gate flag therefore has
        exactly one possible writer by construction. Nesting is still handled, because a
        composed operation may open the gate inside an already-open one.
        """
        previous = self._transition_gate_open
        self._transition_gate_open = True
        try:
            yield
        finally:
            self._transition_gate_open = previous

    @contextmanager
    def status_transition(self) -> Iterator[sqlite3.Connection]:
        """A transaction in which task status may be changed.

        Deliberately the only route to a status update. Callers outside
        :mod:`claude_away.core.state` should never need this.
        """
        with self.transaction() as con, self._open_transition_gate():
            yield con

    # ------------------------------------------------------------------------ helpers

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute a statement, translating SQLite constraint failures into domain errors."""
        try:
            return self._connection.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolationError(str(exc), sql=_summarise_sql(sql)) from exc

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return list(self._connection.execute(sql, params).fetchall())

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        result: sqlite3.Row | None = self._connection.execute(sql, params).fetchone()
        return result

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----------------------------------------------------------------------- migrations

    def migrate(self) -> int:
        """Apply outstanding migrations and return the resulting schema version.

        Idempotent: running it against an up-to-date database is a no-op. Each migration
        commits together with its ledger row, so a crash leaves the database at a known
        version rather than partially upgraded.
        """
        self._ensure_migration_ledger()
        current = self.schema_version()

        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                "database schema is newer than this build understands; refusing to open",
                found=current,
                supported=SCHEMA_VERSION,
                path=str(self.path),
            )

        for version, name, script in _MIGRATIONS:
            if version <= current:
                continue
            try:
                # Deliberately NOT executescript(): it issues an implicit COMMIT before
                # running, which would drop us out of the migration transaction and leave a
                # partially-applied schema with no ledger row if the process died midway.
                # Splitting and executing statement by statement keeps the schema change
                # and its version record in one atomic unit.
                with self.transaction() as con:
                    for statement in _split_statements(script):
                        con.execute(statement)
                    con.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, to_iso(self.clock.now())),
                    )
            except sqlite3.Error as exc:
                raise MigrationError(
                    f"migration {version} ({name}) failed: {exc}",
                    version=version,
                    name=name,
                ) from exc

        return self.schema_version()

    def _ensure_migration_ledger(self) -> None:
        # executescript() would implicitly commit an open transaction, so the ledger table
        # is created with a plain execute outside the migration transaction. It is the one
        # piece of schema that must exist before versioning can begin.
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL"
            ") STRICT"
        )

    def schema_version(self) -> int:
        """Return the highest applied migration version, or ``0`` for a fresh database."""
        self._ensure_migration_ledger()
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"])

    def missing_triggers(self) -> list[str]:
        """Guard triggers that should exist but do not.

        A non-empty result means the database's integrity guarantees have been tampered
        with or a migration did not complete. Either way it is not safe to start an
        unattended run against it.
        """
        present = {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        return sorted(EXPECTED_TRIGGERS - present)

    def applied_migrations(self) -> list[dict[str, Any]]:
        self._ensure_migration_ledger()
        return [
            {"version": r["version"], "name": r["name"], "applied_at": r["applied_at"]}
            for r in self._connection.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            )
        ]


def _is_only_comments(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True


def _split_statements(script: str) -> list[str]:
    """Split a migration script into individually executable statements.

    Uses ``sqlite3.complete_statement``, which wraps SQLite's own parser and therefore
    tracks ``BEGIN ... END`` trigger bodies correctly. Splitting on ``;`` would cut every
    trigger in this schema in half.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            candidate = buffer.strip()
            if candidate and not _is_only_comments(candidate):
                statements.append(candidate)
            buffer = ""
    trailing = buffer.strip()
    if trailing and not _is_only_comments(trailing):
        statements.append(trailing)
    return statements


def _summarise_sql(sql: str) -> str:
    """Compact a statement for error payloads without dumping a whole script."""
    collapsed = " ".join(sql.split())
    return collapsed if len(collapsed) <= 120 else collapsed[:117] + "..."


def dumps(value: Any) -> str:
    """Serialise bounded metadata for storage.

    ``sort_keys`` matters: it makes stored JSON byte-stable, which in turn makes
    idempotency request hashes stable across processes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def open_database(
    path: str | Path, *, clock: Clock | None = None, migrate: bool = True
) -> Database:
    """Open (and by default migrate) a state database."""
    db = Database(path, clock=clock)
    if migrate:
        db.migrate()
    return db
