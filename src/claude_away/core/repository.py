"""Reading and writing the non-status parts of the state database.

Everything here is deliberately status-blind. Creating a task sets its initial status
exactly once, at insert time; from then on the *only* code that may change a status is
:mod:`claude_away.core.state`, and the database enforces that with a trigger rather than
trusting this separation to hold by convention.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from claude_away.clock import parse_timestamp, to_iso
from claude_away.core.db import Database, dumps
from claude_away.core.evidence import verification_spec_hash
from claude_away.core.models import (
    EstimatedEffort,
    Risk,
    Task,
    TaskStatus,
    VerificationRequirement,
    VerificationType,
)
from claude_away.core.validation import (
    validate_task_document,
    validate_verification_contract,
)
from claude_away.errors import NotFoundError, TaskLeasedError, ValidationError

__all__ = [
    "create_goal",
    "create_plan_version",
    "create_project",
    "create_task",
    "current_plan_version",
    "get_task",
    "list_tasks",
    "runner_id",
    "task_nodes",
    "update_verification_requirements",
]


# ======================================================================================
# Controller identity
# ======================================================================================


def runner_id(db: Database) -> str:
    """Return this state database's stable runner id, generating it on first use.

    Bound to the database rather than to configuration on purpose: a config file gets
    copied between machines, and two runners sharing an owner id would quietly steal each
    other's leases -- the exact failure the lease exists to prevent.
    """
    row = db.query_one("SELECT value FROM meta WHERE key = 'runner_id'")
    if row is not None:
        return str(row["value"])

    generated = f"runner-{secrets.token_hex(8)}"
    with db.transaction() as con:
        # Another process may have raced us here; the primary key makes the winner
        # unambiguous and we simply adopt whatever was stored.
        con.execute(
            "INSERT OR IGNORE INTO meta(key, value, created_at) VALUES ('runner_id', ?, ?)",
            (generated, to_iso(db.clock.now())),
        )
        stored = con.execute("SELECT value FROM meta WHERE key = 'runner_id'").fetchone()
    return str(stored["value"])


# ======================================================================================
# Projects, goals, plan versions
# ======================================================================================


def create_project(
    db: Database,
    project_id: str,
    *,
    path: str | None = None,
    repository: str | None = None,
    default_branch: str | None = None,
) -> None:
    with db.transaction() as con:
        con.execute(
            "INSERT INTO projects(id, path, repository, default_branch, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, path, repository, default_branch, to_iso(db.clock.now())),
        )


def create_goal(
    db: Database,
    goal_id: str,
    *,
    title: str,
    priority: int,
    success_criteria: Sequence[str],
) -> None:
    if not success_criteria:
        raise ValidationError("a goal needs at least one success criterion", goal_id=goal_id)
    with db.transaction() as con:
        con.execute(
            "INSERT INTO goals(id, title, priority, created_at) VALUES (?, ?, ?, ?)",
            (goal_id, title, priority, to_iso(db.clock.now())),
        )
        con.executemany(
            "INSERT INTO goal_success_criteria(goal_id, position, text) VALUES (?, ?, ?)",
            [(goal_id, index, text) for index, text in enumerate(success_criteria)],
        )


def create_plan_version(
    db: Database, *, reason: str = "", summary: Mapping[str, Any] | None = None
) -> int:
    """Allocate the next monotonic plan version."""
    with db.transaction() as con:
        row = con.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM plan_versions"
        ).fetchone()
        version = int(row["next"])
        con.execute(
            "INSERT INTO plan_versions(version, created_at, reason, summary) VALUES (?, ?, ?, ?)",
            (version, to_iso(db.clock.now()), reason, dumps(dict(summary or {}))),
        )
    return version


def current_plan_version(db: Database) -> int:
    row = db.query_one("SELECT COALESCE(MAX(version), 0) AS v FROM plan_versions")
    return 0 if row is None else int(row["v"])


# ======================================================================================
# Tasks
# ======================================================================================


def create_task(db: Database, document: Mapping[str, Any]) -> Task:
    """Persist a validated task record.

    The document is validated first -- schema *and* cross-property invariants -- so a task
    that could never reach ``DONE`` (no required verification, or only an LLM review
    gating it) is refused before it can occupy a slot in the plan.
    """
    validate_task_document(document)

    task_id = str(document["id"])
    now = to_iso(db.clock.now())
    created_at = str(document.get("createdAt") or now)
    updated_at = str(document.get("updatedAt") or now)
    status_changed_at = str(document.get("statusChangedAt") or now)

    with db.transaction() as con:
        if (
            con.execute(
                "SELECT 1 FROM projects WHERE id = ?", (str(document["projectId"]),)
            ).fetchone()
            is None
        ):
            raise NotFoundError("project", str(document["projectId"]))

        for goal_id in document["goalIds"]:
            if con.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is None:
                raise NotFoundError("goal", str(goal_id))

        con.execute(
            "INSERT INTO tasks("
            "  id, project_id, title, description, status, priority, risk,"
            "  estimated_effort, human_required, created_by_plan_version,"
            "  updated_by_plan_version, created_at, updated_at, status_changed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                str(document["projectId"]),
                str(document["title"]),
                str(document["description"]),
                str(document["status"]),
                int(document["priority"]),
                str(document["risk"]),
                str(document["estimatedEffort"]),
                1 if document["humanRequired"] else 0,
                int(document["createdByPlanVersion"]),
                int(document["updatedByPlanVersion"]),
                created_at,
                updated_at,
                status_changed_at,
            ),
        )

        con.executemany(
            "INSERT INTO task_goals(task_id, goal_id) VALUES (?, ?)",
            [(task_id, goal_id) for goal_id in document["goalIds"]],
        )
        con.executemany(
            "INSERT INTO task_dependencies(task_id, depends_on_id) VALUES (?, ?)",
            [(task_id, dependency) for dependency in document.get("dependencies", ())],
        )
        con.executemany(
            "INSERT INTO acceptance_criteria(task_id, criterion_id, position, text) "
            "VALUES (?, ?, ?, ?)",
            [
                (task_id, str(criterion["id"]), index, str(criterion["text"]))
                for index, criterion in enumerate(document["acceptanceCriteria"])
            ],
        )
        con.executemany(
            "INSERT INTO expected_artifacts(task_id, position, path) VALUES (?, ?, ?)",
            [
                (task_id, index, str(path))
                for index, path in enumerate(document.get("expectedArtifacts", ()))
            ],
        )

        for position, requirement in enumerate(document["verification"]):
            _insert_requirement(con, task_id, position, requirement)

    loaded = get_task(db, task_id)
    assert loaded is not None
    return loaded


def _insert_requirement(
    con: Any, task_id: str, position: int, requirement: Mapping[str, Any]
) -> None:
    verification_type = VerificationType(str(requirement["type"]))
    command = requirement.get("command")
    path = requirement.get("path")
    description = requirement.get("description")
    con.execute(
        "INSERT INTO verification_requirements("
        "  task_id, verification_id, position, type, required, command, path,"
        "  description, spec_hash"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            str(requirement["id"]),
            position,
            verification_type.value,
            1 if requirement["required"] else 0,
            command,
            path,
            description,
            verification_spec_hash(
                type=verification_type,
                command=command,
                path=path,
                description=description,
            ),
        ),
    )


def update_verification_requirements(
    db: Database,
    task_id: str,
    requirements: Sequence[Mapping[str, Any]],
    *,
    plan_version: int,
    allow_weakening: bool = False,
    justification: str = "",
) -> None:
    """Replace a task's verification contract, refusing silent weakening.

    This is the deterministic half of what a future strategic replan will do. Two rules
    are enforced, and both come straight from the existing documents:

    * **A leased task's contract is frozen.** STATE_MODEL: "replan never mutates a leased
      task's execution contract underneath it." Attempting it raises
      :class:`TaskLeasedError`.
    * **Unproven requirements may not be relaxed.** CONTRIBUTING lists "silent weakening or
      removal of failed acceptance criteria" among the things the project will not accept.
      Dropping a requirement, flipping it from required to optional, or editing its spec is
      refused whenever its latest evidence in the current attempt is absent or not passing.
      Relaxing a requirement that *did* pass is fine, and tightening is always free.

    ``allow_weakening`` exists so a human can override deliberately; it demands a
    justification, and the decision is recorded either way.
    """
    from claude_away.core.leases import active_lease  # local import: avoids a cycle

    now = db.clock.now()
    lease = active_lease(db, task_id)
    if lease is not None and lease.is_live_at(now):
        raise TaskLeasedError(
            "cannot change the verification contract of a leased task",
            task_id=task_id,
            holder=lease.owner_id,
        )

    if allow_weakening and not justification:
        raise ValidationError(
            "overriding the weakening guard requires a justification", task_id=task_id
        )

    # The same contract rules creation enforces. A replan that could install an
    # all-optional or review-only contract would hand back exactly the free DONE that
    # creation refuses -- and allow_weakening must NOT waive this, because it exists to
    # permit retiring a proven check, not to remove the deterministic floor entirely.
    validate_verification_contract(requirements, task_id=task_id)

    with db.transaction() as con:
        existing = {
            str(row["verification_id"]): row
            for row in con.execute(
                "SELECT verification_id, required, spec_hash FROM verification_requirements "
                "WHERE task_id = ?",
                (task_id,),
            )
        }
        attempt_row = con.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND outcome IS NULL", (task_id,)
        ).fetchone()
        attempt_id = None if attempt_row is None else str(attempt_row["id"])

        incoming: dict[str, Mapping[str, Any]] = {str(r["id"]): r for r in requirements}

        if not allow_weakening:
            for verification_id, row in existing.items():
                if not int(row["required"]):
                    continue  # relaxing an already-optional check weakens nothing

                replacement = incoming.get(verification_id)
                removed = replacement is None
                demoted = replacement is not None and not bool(replacement["required"])
                respecified = False
                if replacement is not None:
                    respecified = verification_spec_hash(
                        type=VerificationType(str(replacement["type"])),
                        command=replacement.get("command"),
                        path=replacement.get("path"),
                        description=replacement.get("description"),
                    ) != str(row["spec_hash"])

                if not (removed or demoted or respecified):
                    continue

                latest = con.execute(
                    "SELECT result FROM evidence "
                    " WHERE task_id = ? AND verification_id = ? AND requirement_spec_hash = ? "
                    "   AND attempt_id IS ? ORDER BY id DESC LIMIT 1",
                    (task_id, verification_id, str(row["spec_hash"]), attempt_id),
                ).fetchone()
                proven = latest is not None and str(latest["result"]) == "pass"
                if not proven:
                    raise ValidationError(
                        "refusing to weaken an unproven required verification; pass "
                        "allow_weakening with a justification to override deliberately",
                        task_id=task_id,
                        verification_id=verification_id,
                        change="removed" if removed else ("demoted" if demoted else "respecified"),
                    )

        con.execute("DELETE FROM verification_requirements WHERE task_id = ?", (task_id,))
        for position, requirement in enumerate(requirements):
            _insert_requirement(con, task_id, position, requirement)

        con.execute(
            "UPDATE tasks SET updated_by_plan_version = ?, updated_at = ? WHERE id = ?",
            (plan_version, to_iso(now), task_id),
        )
        con.execute(
            "INSERT INTO events(created_at, kind, task_id, actor, payload) "
            "VALUES (?, 'verification_contract_updated', ?, 'planner', ?)",
            (
                to_iso(now),
                task_id,
                dumps(
                    {
                        "plan_version": plan_version,
                        "allow_weakening": allow_weakening,
                        "justification": justification,
                        "verification_ids": sorted(incoming),
                    }
                ),
            ),
        )


def get_task(db: Database, task_id: str) -> Task | None:
    row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        return None

    goal_ids = tuple(
        str(r["goal_id"])
        for r in db.query(
            "SELECT goal_id FROM task_goals WHERE task_id = ? ORDER BY goal_id", (task_id,)
        )
    )
    dependencies = tuple(
        str(r["depends_on_id"])
        for r in db.query(
            "SELECT depends_on_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_id",
            (task_id,),
        )
    )
    criteria = tuple(
        str(r["text"])
        for r in db.query(
            "SELECT text FROM acceptance_criteria WHERE task_id = ? ORDER BY position",
            (task_id,),
        )
    )
    requirements = tuple(
        VerificationRequirement(
            id=str(r["verification_id"]),
            type=VerificationType(str(r["type"])),
            required=bool(r["required"]),
            spec_hash=str(r["spec_hash"]),
            command=r["command"],
            path=r["path"],
            description=r["description"],
        )
        for r in db.query(
            "SELECT verification_id, type, required, command, path, description, spec_hash "
            "FROM verification_requirements WHERE task_id = ? ORDER BY position",
            (task_id,),
        )
    )

    return Task(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        status=TaskStatus(str(row["status"])),
        priority=int(row["priority"]),
        risk=Risk(str(row["risk"])),
        estimated_effort=EstimatedEffort(str(row["estimated_effort"])),
        human_required=bool(row["human_required"]),
        created_by_plan_version=int(row["created_by_plan_version"]),
        updated_by_plan_version=int(row["updated_by_plan_version"]),
        created_at=parse_timestamp(str(row["created_at"])),
        updated_at=parse_timestamp(str(row["updated_at"])),
        goal_ids=goal_ids,
        dependencies=dependencies,
        acceptance_criteria=criteria,
        verification=requirements,
    )


def list_tasks(db: Database, *, status: TaskStatus | None = None) -> list[Task]:
    sql = "SELECT id FROM tasks"
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status.value)
    sql += " ORDER BY id"
    tasks = []
    for row in db.query(sql, params):
        task = get_task(db, str(row["id"]))
        if task is not None:
            tasks.append(task)
    return tasks


def task_nodes(db: Database) -> dict[str, Any]:
    """Load the whole graph in the compact form :mod:`claude_away.core.dag` expects.

    One query per relation rather than one per task: readiness recomputation runs after
    every terminal task event, so an N+1 here would show up as real latency on a large
    plan.
    """
    from claude_away.core.dag import TaskNode

    statuses = {
        str(row["id"]): TaskStatus(str(row["status"]))
        for row in db.query("SELECT id, status FROM tasks")
    }
    edges: dict[str, list[str]] = {task_id: [] for task_id in statuses}
    for row in db.query(
        "SELECT task_id, depends_on_id FROM task_dependencies ORDER BY task_id, depends_on_id"
    ):
        edges[str(row["task_id"])].append(str(row["depends_on_id"]))

    return {
        task_id: TaskNode(id=task_id, status=status, dependencies=tuple(edges.get(task_id, ())))
        for task_id, status in statuses.items()
    }
