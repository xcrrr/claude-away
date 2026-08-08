"""Shared fixtures.

Every test runs against a real on-disk SQLite database rather than ``:memory:``. That is a
deliberate cost: WAL mode, file locking and cross-connection visibility are exactly the
behaviours the lease and recovery tests need to exercise, and an in-memory database gives
each connection its own private universe -- which would make the concurrency tests pass
while proving nothing.

Time is always a :class:`~claude_away.clock.ManualClock`. Lease expiry is tested by moving
the clock, never by sleeping.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_away.clock import ManualClock
from claude_away.core import repository as repo
from claude_away.core.db import Database, open_database
from claude_away.core.models import Task

TIMESTAMP = "2026-01-01T00:00:00.000000+00:00"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def db(db_path: Path, clock: ManualClock) -> Iterator[Database]:
    database = open_database(db_path, clock=clock)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def seeded(db: Database) -> Database:
    """A database with one project, one goal and an initial plan version."""
    repo.create_project(db, "api", path="/tmp/api", default_branch="main")
    repo.create_goal(db, "ship", title="Ship v1", priority=90, success_criteria=["all tests pass"])
    repo.create_plan_version(db, reason="initial plan")
    return db


def task_document(
    task_id: str,
    *,
    dependencies: Sequence[str] = (),
    verification: Sequence[Mapping[str, Any]] | None = None,
    status: str = "PENDING",
    plan_version: int = 1,
    human_required: bool = False,
    priority: int = 50,
) -> dict[str, Any]:
    """Build a valid task record.

    Defaults to a single required ``command`` check, which is the minimum the validator
    accepts: a task with no required deterministic verification could never legitimately
    reach ``DONE``.
    """
    if verification is None:
        verification = [
            {"id": "unit-tests", "type": "command", "required": True, "command": "pytest -q"}
        ]
    return {
        "schemaVersion": 1,
        "id": task_id,
        "goalIds": ["ship"],
        "projectId": "api",
        "title": f"Task {task_id}",
        "description": "A task used in tests.",
        "dependencies": list(dependencies),
        "priority": priority,
        "risk": "low",
        "estimatedEffort": "small",
        "acceptanceCriteria": [{"id": "works", "text": "It works."}],
        "verification": list(verification),
        "humanRequired": human_required,
        "status": status,
        "createdByPlanVersion": plan_version,
        "updatedByPlanVersion": plan_version,
        "createdAt": TIMESTAMP,
        "updatedAt": TIMESTAMP,
        "statusChangedAt": TIMESTAMP,
    }


def config_document(**overrides: Any) -> dict[str, Any]:
    """A minimal valid configuration document."""
    document: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "local",
        "stateDbPath": ".claude-away/state.db",
        "projects": [{"id": "api", "path": "/srv/api", "defaultBranch": "main"}],
        "goals": [{"id": "ship", "title": "Ship v1", "priority": 90, "successCriteria": ["green"]}],
        "capacity": {
            "fiveHourTargetPercent": 95,
            "weeklyTargetPercent": 97,
            "noBusywork": True,
        },
        "planning": {
            "strategicReplanHours": 84,
            "microReplan": True,
            "regenerateWhenEmpty": True,
        },
        "execution": {
            "maxAttemptsPerTask": 3,
            "maxConcurrentTasks": 1,
            "leaseSeconds": 1800,
            "leaseHeartbeatSeconds": 60,
        },
        "safety": {
            "allowCommit": True,
            "allowPush": False,
            "allowMerge": False,
            "allowDeploy": False,
            "allowDestructive": False,
            "protectedPaths": [],
        },
        "brain": {
            "enabled": True,
            "root": ".claude-away/brain",
            "obsidian": True,
            "graphify": "auto",
        },
    }
    document.update(overrides)
    return document


def load_task(db: Database, task_id: str) -> Task:
    """Fetch a task that the test knows exists.

    Keeps assertions readable without sprinkling `assert task is not None` at every call
    site, while still failing loudly (rather than with an AttributeError) if it is absent.
    """
    task = repo.get_task(db, task_id)
    assert task is not None, f"expected task {task_id} to exist"
    return task
