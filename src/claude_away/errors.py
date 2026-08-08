"""Typed domain errors for the Claude Away deterministic core.

Claude Away is designed to run unattended for days. A supervisor recovering from a
crash must be able to understand *what* went wrong without parsing English prose, so
every domain failure carries:

* a stable machine-readable ``code`` (never localise or reword these -- they are a
  compatibility surface for the future supervisor and for ``awayctl --json``);
* a structured ``details`` mapping containing the specific identifiers involved;
* a human-readable message for logs and terminal output.

The rule for adding an error: if the supervisor would ever need to branch on it,
it deserves its own class and code. If it is a programming bug, use a builtin.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BaseRevisionError",
    "ClaudeAwayError",
    "DagError",
    "DatabaseError",
    "DependencyCycleError",
    "DuplicateDependencyError",
    "DuplicateEnrolmentError",
    "EnrolmentError",
    "EvidenceImmutableError",
    "EvidenceIncompleteError",
    "GitCommandError",
    "GitError",
    "GitOutputError",
    "IdempotencyConflictError",
    "IllegalTransitionError",
    "IntegrityViolationError",
    "InvalidStateError",
    "LeaseConflictError",
    "LeaseError",
    "LeaseExpiredError",
    "LeaseNotHeldError",
    "MigrationError",
    "MissingDependencyError",
    "NotAGitRepositoryError",
    "NotEnrolledError",
    "NotFoundError",
    "PlanVersionError",
    "PolicyDeniedError",
    "ReconciliationRequiredError",
    "SchemaValidationError",
    "SchemaVersionError",
    "SelfDependencyError",
    "StaleReplayError",
    "TaskLeasedError",
    "UnsafeStateLocationError",
    "UnsupportedRepositoryError",
    "ValidationError",
]


class ClaudeAwayError(Exception):
    """Base class for every domain error raised by the deterministic core.

    ``code`` is the stable machine identifier. Subclasses set a class-level default;
    instances must not override it with anything unstable.
    """

    code = "claude_away_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        # Sorted so that serialised errors are byte-stable across runs, which makes
        # them safe to compare in tests and to diff in logs.
        self.details: dict[str, Any] = dict(sorted(details.items()))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe mapping for ``--json`` output and event payloads."""
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in self.details.items())
        return f"{self.message} ({rendered})"


# --------------------------------------------------------------------------------------
# Storage and migrations
# --------------------------------------------------------------------------------------


class DatabaseError(ClaudeAwayError):
    """Generic storage-layer failure that is not more specifically classified."""

    code = "database_error"


class MigrationError(DatabaseError):
    """A migration could not be applied, or the migration ledger is inconsistent."""

    code = "migration_error"


class SchemaVersionError(DatabaseError):
    """The on-disk schema version is not one this build can operate on.

    Most importantly this covers a *newer* database opened by an *older* build. Refusing
    is mandatory: silently operating on a schema we do not understand is exactly how an
    unattended system corrupts a week of work.
    """

    code = "schema_version_error"


class IntegrityViolationError(DatabaseError):
    """A database-level invariant (constraint, trigger, foreign key) rejected a write.

    This is raised when the storage layer's own guards fire. Seeing it means an
    application-level guard was missing or bypassed, so it is always worth investigating.
    """

    code = "integrity_violation"


# --------------------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------------------


class NotFoundError(ClaudeAwayError):
    """A referenced entity does not exist."""

    code = "not_found"

    def __init__(self, kind: str, identifier: str) -> None:
        super().__init__(f"{kind} {identifier!r} does not exist", kind=kind, id=identifier)
        self.kind = kind
        self.identifier = identifier


# --------------------------------------------------------------------------------------
# Validation (schema and cross-object)
# --------------------------------------------------------------------------------------


class ValidationError(ClaudeAwayError):
    """A document or object graph violated a deterministic validation rule."""

    code = "validation_error"


class SchemaValidationError(ValidationError):
    """A document failed JSON Schema validation.

    ``errors`` holds one entry per violation so callers can report all problems at once
    rather than making a user fix them one at a time.
    """

    code = "schema_validation_error"

    def __init__(self, message: str, errors: list[dict[str, Any]], **details: Any) -> None:
        super().__init__(message, errors=errors, **details)
        self.errors = errors


class PlanVersionError(ValidationError):
    """A plan-version relationship is invalid (for example ``updatedBy < createdBy``)."""

    code = "plan_version_error"


# --------------------------------------------------------------------------------------
# DAG
# --------------------------------------------------------------------------------------


class DagError(ValidationError):
    """Base class for task-graph validation failures."""

    code = "dag_error"


class MissingDependencyError(DagError):
    """A task depends on a task that does not exist in the graph."""

    code = "missing_dependency"

    def __init__(self, task_id: str, missing: list[str]) -> None:
        rendered = ", ".join(sorted(missing))
        super().__init__(
            f"task {task_id!r} depends on unknown task(s): {rendered}",
            task_id=task_id,
            missing=sorted(missing),
        )


class SelfDependencyError(DagError):
    """A task lists itself as a dependency."""

    code = "self_dependency"

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id!r} depends on itself", task_id=task_id)


class DuplicateDependencyError(DagError):
    """A task lists the same dependency more than once."""

    code = "duplicate_dependency"

    def __init__(self, task_id: str, duplicated: list[str]) -> None:
        rendered = ", ".join(sorted(duplicated))
        super().__init__(
            f"task {task_id!r} lists duplicate dependencies: {rendered}",
            task_id=task_id,
            duplicated=sorted(duplicated),
        )


class DependencyCycleError(DagError):
    """The task graph contains a cycle.

    ``cycle`` is the actual node path, closed (first element repeated last), so the
    diagnostic points at the real problem instead of merely asserting that a cycle exists.
    """

    code = "dependency_cycle"

    def __init__(self, cycle: list[str]) -> None:
        super().__init__("task dependency cycle detected: " + " -> ".join(cycle), cycle=cycle)
        self.cycle = cycle


# --------------------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------------------


class InvalidStateError(ClaudeAwayError):
    """An operation was attempted against a task in an incompatible state."""

    code = "invalid_state"


class IllegalTransitionError(ClaudeAwayError):
    """A state transition is not permitted by the documented transition table."""

    code = "illegal_transition"

    def __init__(self, task_id: str, from_status: str, to_status: str, reason: str = "") -> None:
        message = f"illegal transition for task {task_id!r}: {from_status} -> {to_status}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(
            message,
            task_id=task_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
        )
        self.from_status = from_status
        self.to_status = to_status


class EvidenceIncompleteError(ClaudeAwayError):
    """The evidence gate refused ``VERIFYING -> DONE``.

    This is *the* invariant of the project: DONE is a claim backed by evidence. The
    payload names precisely which required verification requirements are unsatisfied so
    that no caller has to guess, and so a future supervisor can decide whether to retry.
    """

    code = "evidence_incomplete"

    def __init__(
        self,
        task_id: str,
        attempt_id: str | None,
        missing: list[str],
        failed: list[str],
    ) -> None:
        parts: list[str] = []
        if missing:
            parts.append(f"missing evidence for {', '.join(sorted(missing))}")
        if failed:
            parts.append(f"failing evidence for {', '.join(sorted(failed))}")
        detail = "; ".join(parts) if parts else "required verification is unsatisfied"
        super().__init__(
            f"task {task_id!r} cannot be marked DONE: {detail}",
            task_id=task_id,
            attempt_id=attempt_id,
            missing=sorted(missing),
            failed=sorted(failed),
        )
        self.missing = sorted(missing)
        self.failed = sorted(failed)


class EvidenceImmutableError(ClaudeAwayError):
    """An attempt was made to mutate or delete recorded evidence.

    Evidence is append-only history. Failed evidence in particular must survive, because
    it is the record that stops a later run from pretending verification succeeded.
    """

    code = "evidence_immutable"


# --------------------------------------------------------------------------------------
# Leases and concurrency
# --------------------------------------------------------------------------------------


class LeaseError(ClaudeAwayError):
    """Base class for lease failures."""

    code = "lease_error"


class LeaseConflictError(LeaseError):
    """Another owner currently holds a live lease on this task."""

    code = "lease_conflict"

    def __init__(self, task_id: str, holder: str, requester: str, expires_at: str) -> None:
        super().__init__(
            f"task {task_id!r} is leased by {holder!r} until {expires_at}",
            task_id=task_id,
            holder=holder,
            requester=requester,
            expires_at=expires_at,
        )
        self.holder = holder
        self.requester = requester


class LeaseNotHeldError(LeaseError):
    """The caller tried to renew or release a lease it does not own."""

    code = "lease_not_held"


class LeaseExpiredError(LeaseError):
    """The caller's lease expired before the operation was attempted."""

    code = "lease_expired"


class TaskLeasedError(ClaudeAwayError):
    """A structural change was refused because the task is actively leased.

    A future strategic replan may not rewrite the execution contract underneath a task
    that a runner is currently working on.
    """

    code = "task_leased"


class ReconciliationRequiredError(ClaudeAwayError):
    """An expired lease was found; the task needs reconciliation before it may run again.

    An expired lease explicitly does **not** mean "safe to rerun". The previous runner may
    have committed work, pushed a branch, or half-finished an external action before it
    died. Something must inspect the repository and attempt checkpoint first.
    """

    code = "reconciliation_required"


# --------------------------------------------------------------------------------------
# Git and enrolment
# --------------------------------------------------------------------------------------


class GitError(ClaudeAwayError):
    """A Git invocation failed or returned something we refuse to interpret."""

    code = "git_error"


class GitCommandError(GitError):
    """``git`` exited non-zero.

    Carries the argv, exit status and a bounded slice of stderr. Deliberately not the
    environment: a future supervisor needs to know which command failed, not what was in
    the process environment when it did.
    """

    code = "git_command_failed"

    def __init__(
        self, argv: list[str], returncode: int, stderr: str, cwd: str | None = None
    ) -> None:
        super().__init__(
            f"git {' '.join(argv[1:3])} failed with exit code {returncode}",
            argv=argv,
            returncode=returncode,
            stderr=stderr,
            cwd=cwd,
        )
        self.argv = argv
        self.returncode = returncode


class GitOutputError(GitError):
    """Git produced output this build will not parse.

    Raised rather than guessed at. A status parser that silently drops an entry it does not
    understand would report a dirty repository as clean, which is the direction that gets
    work committed on top of somebody else's changes.
    """

    code = "git_output_unparseable"


class UnsupportedRepositoryError(GitError):
    """The path is a Git repository of a kind Claude Away does not operate on."""

    code = "unsupported_repository"


class NotAGitRepositoryError(GitError):
    """The path exists but is not inside a Git working tree."""

    code = "not_a_git_repository"


class EnrolmentError(ClaudeAwayError):
    """A configured project could not be turned into a usable enrolled repository."""

    code = "enrolment_error"


class NotEnrolledError(EnrolmentError):
    """An operation named a repository that was never explicitly enrolled.

    The default answer for any path the user did not list. Discovering a repository is not
    the same as being allowed to touch it.
    """

    code = "not_enrolled"


class DuplicateEnrolmentError(EnrolmentError):
    """Two project ids resolve to the same canonical repository.

    Refused because locks, leases and branch names are keyed by project id: two ids for one
    working tree would let two tasks believe they had exclusive access to it.
    """

    code = "duplicate_enrolment"


class UnsafeStateLocationError(EnrolmentError):
    """The state database would live inside an enrolled repository."""

    code = "unsafe_state_location"


class BaseRevisionError(ClaudeAwayError):
    """The expected base revision could not be established unambiguously."""

    code = "base_revision_unresolved"


# --------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------


class PolicyDeniedError(ClaudeAwayError):
    """A requested operation was refused by the deterministic safety policy."""

    code = "policy_denied"

    def __init__(self, operation: str, rule: str, reason: str, **details: Any) -> None:
        super().__init__(
            f"operation {operation!r} denied by rule {rule!r}: {reason}",
            operation=operation,
            rule=rule,
            reason=reason,
            **details,
        )
        self.operation = operation
        self.rule = rule


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


class StaleReplayError(ClaudeAwayError):
    """A recorded idempotent result no longer describes reality.

    Distinct from :class:`IdempotencyConflictError`, which means "you reused a key for
    different content". This one means "your key is right, but the world moved on since" --
    the task was failed or cancelled, or the attempt was closed. A recovering supervisor
    must re-read state rather than trust the old answer, and those are different responses.
    """

    code = "stale_replay"


class IdempotencyConflictError(ClaudeAwayError):
    """An idempotency key was reused with a materially different request.

    Replaying an identical operation after a crash is safe and returns the original
    result. Reusing the same key for *different* content is a bug in the caller and is
    rejected loudly rather than silently picking a winner.
    """

    code = "idempotency_conflict"

    def __init__(self, key: str, operation: str) -> None:
        super().__init__(
            f"idempotency key {key!r} was already used for a different {operation!r} request",
            key=key,
            operation=operation,
        )
        self.key = key
        self.operation = operation
