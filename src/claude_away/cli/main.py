"""``awayctl`` -- the deterministic controller's command line.

Scope note: this is an *inspection and bootstrap* surface, not the user-facing product.
The ``/claude-away:*`` skills arrive in Milestone 5 and will call this controller rather
than touching SQLite themselves.

One omission is deliberate and permanent. There is no command that sets a task status, and
there will not be one. A verb like ``awayctl task set-status --done`` would be convenient
during development and would also be a complete bypass of the evidence gate -- the single
thing this milestone exists to make impossible. Status changes happen only as a
consequence of real events, through :mod:`claude_away.core.state`.

Every command supports ``--json``. An unattended supervisor should never have to parse
prose, and exit codes are stable: ``0`` success, ``1`` unexpected error, ``2`` usage,
``3`` a domain error (the payload names which).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from claude_away import __version__
from claude_away.adapters.git import inspect_repository
from claude_away.core.base_revision import resolve_expected_base
from claude_away.core.dag import blocking_dependencies, find_cycle
from claude_away.core.db import SCHEMA_VERSION, Database, open_database
from claude_away.core.enrolment import enrol_projects
from claude_away.core.evidence import evaluate_gate
from claude_away.core.leases import active_lease, expired_leases
from claude_away.core.models import TaskStatus
from claude_away.core.policy import Operation, SafetyPolicy
from claude_away.core.repository import list_tasks, runner_id, task_nodes
from claude_away.core.state import active_attempt, list_attempts
from claude_away.core.validation import validate_config_document
from claude_away.errors import ClaudeAwayError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DOMAIN = 3

DEFAULT_DB_PATH = Path(".claude-away") / "state.db"


def _emit(payload: Any, *, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(human)


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    env = os.environ.get("CLAUDE_AWAY_DB")
    return Path(env) if env else DEFAULT_DB_PATH


def _open(args: argparse.Namespace, *, migrate: bool = False) -> Database:
    path = _resolve_db_path(args)
    if not path.exists() and not migrate:
        raise ClaudeAwayError(
            f"no state database at {path}; run 'awayctl init' first", path=str(path)
        )
    return open_database(path, migrate=True)


# ======================================================================================
# Commands
# ======================================================================================


def cmd_version(args: argparse.Namespace) -> int:
    payload = {"version": __version__, "schema_version": SCHEMA_VERSION}
    _emit(
        payload,
        as_json=args.json,
        human=f"claude-away {__version__} (state schema v{SCHEMA_VERSION})",
    )
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    """Create and migrate a state database."""
    path = _resolve_db_path(args)
    existed = path.exists()
    db = open_database(path, migrate=True)
    try:
        # The state database is the authority for a multi-day unattended run. Keep it
        # readable only by its owner, and keep it outside any enrolled repository so that
        # an agent working in a repo cannot reach it.
        if path.exists():
            path.chmod(0o600)
        identity = runner_id(db)
        payload = {
            "path": str(path.resolve()),
            "created": not existed,
            "schema_version": db.schema_version(),
            "runner_id": identity,
            "migrations": db.applied_migrations(),
        }
    finally:
        db.close()

    verb = "reused" if existed else "created"
    _emit(
        payload,
        as_json=args.json,
        human=(
            f"{verb} state database at {payload['path']}\n"
            f"  schema version : {payload['schema_version']}\n"
            f"  runner id      : {identity}"
        ),
    )
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Validate the persisted state and report problems without changing anything."""
    db = _open(args)
    try:
        problems: list[dict[str, Any]] = []

        # Checked first: if the guard triggers are gone, nothing else in this report can
        # be trusted, because any value in the database could have been written by hand.
        missing = db.missing_triggers()
        if missing:
            problems.append({"kind": "missing_integrity_triggers", "triggers": missing})

        nodes = task_nodes(db)

        cycle = find_cycle(nodes)
        if cycle is not None:
            problems.append({"kind": "dependency_cycle", "cycle": cycle})

        for task_id, node in sorted(nodes.items()):
            report = blocking_dependencies(task_id, nodes)
            if node.status is TaskStatus.READY and not report.is_satisfied:
                problems.append(
                    {
                        "kind": "ready_with_unsatisfied_dependencies",
                        "task_id": task_id,
                        "unsatisfied": list(report.unsatisfied),
                    }
                )
            if report.is_permanently_blocked and node.status not in (
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
                TaskStatus.DONE,
                TaskStatus.BLOCKED,
            ):
                problems.append(
                    {
                        "kind": "depends_on_unsatisfiable_task",
                        "task_id": task_id,
                        "unsatisfiable": list(report.unsatisfiable),
                    }
                )

        stale = expired_leases(db)
        for lease in stale:
            problems.append(
                {
                    "kind": "expired_lease_requires_reconciliation",
                    "task_id": lease.task_id,
                    "holder": lease.owner_id,
                    "expired_at": lease.expires_at.isoformat(),
                }
            )

        # A RUNNING or VERIFYING task with no live attempt is NOT corruption: it is what
        # suspend_attempt leaves behind on a rate limit, a pause or a clean shutdown, and
        # STATE_MODEL treats that as a supported state. It does need attention -- the task
        # cannot progress until a new attempt starts -- so it is reported, but as the
        # resumable condition it actually is rather than as a broken invariant.
        for task_id, node in sorted(nodes.items()):
            if (
                node.status in (TaskStatus.RUNNING, TaskStatus.VERIFYING)
                and active_attempt(db, task_id) is None
            ):
                problems.append(
                    {
                        "kind": "task_awaiting_resume",
                        "task_id": task_id,
                        "status": node.status.value,
                        "detail": (
                            "attempt was suspended or closed; the task needs a new attempt "
                            "before it can progress"
                        ),
                    }
                )

        payload = {
            "healthy": not problems,
            "schema_version": db.schema_version(),
            "task_count": len(nodes),
            "problems": problems,
        }
    finally:
        db.close()

    if args.json:
        _emit(payload, as_json=True, human="")
    elif not problems:
        print(
            f"state is healthy ({payload['task_count']} tasks, schema v{payload['schema_version']})"
        )
    else:
        print(f"found {len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem['kind']}: {json.dumps(problem, sort_keys=True, default=str)}")
    return EXIT_OK if not problems else EXIT_DOMAIN


def cmd_status(args: argparse.Namespace) -> int:
    """Summarise the plan: counts by status, plus per-task detail."""
    db = _open(args)
    try:
        tasks = list_tasks(db)
        counts: dict[str, int] = {status.value: 0 for status in TaskStatus}
        for task in tasks:
            counts[task.status.value] += 1

        rows: list[dict[str, Any]] = []
        for task in tasks:
            attempt = active_attempt(db, task.id)
            lease = active_lease(db, task.id)
            rows.append(
                {
                    "id": task.id,
                    "status": task.status.value,
                    "title": task.title,
                    "project_id": task.project_id,
                    "priority": task.priority,
                    "risk": task.risk.value,
                    "human_required": task.human_required,
                    "dependencies": list(task.dependencies),
                    "attempts": len(list_attempts(db, task.id)),
                    "active_attempt": None if attempt is None else attempt.id,
                    "lease_owner": None if lease is None else lease.owner_id,
                }
            )

        payload = {
            "schema_version": db.schema_version(),
            "counts": counts,
            "tasks": rows,
        }
    finally:
        db.close()

    if args.json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK

    active = {k: v for k, v in counts.items() if v}
    print("status: " + (", ".join(f"{k}={v}" for k, v in sorted(active.items())) or "no tasks"))
    for row in rows:
        marker = "*" if row["active_attempt"] else " "
        print(f" {marker} {row['id']}  {row['status']:<10} {row['title']}")
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    """Show why a task can or cannot be completed. Read-only.

    Useful on its own, and the shape a ``TaskCompleted`` hook will consume in Milestone 3
    as a second, independent enforcement layer.
    """
    db = _open(args)
    try:
        attempt = active_attempt(db, args.task_id)
        report = evaluate_gate(db, args.task_id, attempt_id=None if attempt is None else attempt.id)
        payload = report.to_dict()
    finally:
        db.close()

    if args.json:
        _emit(payload, as_json=True, human="")
    else:
        verdict = "OPEN" if report.satisfied else "CLOSED"
        print(f"evidence gate for {args.task_id}: {verdict} ({report.reason.value})")
        print(f"  required requirements : {report.required_total}")
        if report.satisfied_ids:
            print(f"  satisfied             : {', '.join(report.satisfied_ids)}")
        if report.missing:
            print(f"  missing               : {', '.join(report.missing)}")
        if report.stale:
            print(f"  stale (redefined)     : {', '.join(report.stale)}")
        if report.failed:
            print(f"  failing               : {', '.join(report.failed)}")
        if report.optional_failed:
            print(f"  optional failing      : {', '.join(report.optional_failed)}")
    return EXIT_OK if report.satisfied else EXIT_DOMAIN


def cmd_validate_config(args: argparse.Namespace) -> int:
    """Validate a configuration document against the schema and the deterministic rules."""
    path = Path(args.path)
    document = json.loads(path.read_text(encoding="utf-8"))
    validate_config_document(document)
    _emit(
        {"valid": True, "path": str(path)},
        as_json=args.json,
        human=f"{path} is valid",
    )
    return EXIT_OK


def cmd_repos(args: argparse.Namespace) -> int:
    """Inspect every enrolled repository. Strictly read-only.

    Exercises the whole Milestone 2A boundary end to end: enrolment canonicalises and
    authorises the paths, Git inspection describes them, and base resolution says whether
    each one could be branched from. Nothing here writes to a repository.
    """
    config_path = Path(args.config)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config_document(document)

    enrolment = enrol_projects(document, config_dir=config_path.parent)

    repositories: list[dict[str, Any]] = []
    ready = 0
    for repository in enrolment.repositories:
        inspection = inspect_repository(
            repository.root, configured_default_branch=repository.default_branch
        )
        resolution = resolve_expected_base(inspection, project_id=repository.project_id)
        ready += 1 if resolution.resolved else 0
        repositories.append(
            {
                **repository.to_dict(),
                "inspection": inspection.to_dict(),
                "base": resolution.to_dict(),
            }
        )

    payload = {"count": len(repositories), "ready": ready, "repositories": repositories}
    if args.json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK if ready == len(repositories) else EXIT_DOMAIN

    for entry in repositories:
        base = entry["base"]
        mark = "ok " if base["resolved"] else "BLOCKED"
        print(f" {mark} {entry['project_id']}  {entry['root']}")
        inspection_detail = entry["inspection"]
        branch = inspection_detail["branch"] or "(detached)"
        print(f"      branch {branch}  head {str(inspection_detail['head_commit'])[:12]}")
        if base["resolved"]:
            print(f"      base   {base['commit'][:12]} on {base['branch']}")
        else:
            print(f"      refused: {', '.join(base['refusals'])}")
    return EXIT_OK if ready == len(repositories) else EXIT_DOMAIN


def cmd_policy(args: argparse.Namespace) -> int:
    """Print the full allow/deny matrix for a configuration. Pure, no repository access."""
    config_path = Path(args.config)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config_document(document)

    protected_branches = sorted(
        {
            str(project["defaultBranch"])
            for project in document.get("projects", [])
            if project.get("defaultBranch")
        }
    )
    policy = SafetyPolicy.from_config(document, protected_branches=protected_branches)

    decisions = [policy.evaluate(operation).to_dict() for operation in Operation]
    payload = {
        "protected_branches": protected_branches,
        "protected_paths": list(policy.protected_paths),
        "decisions": decisions,
    }
    if args.json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK

    print(f"protected branches: {', '.join(protected_branches) or '(none)'}")
    print(f"protected paths   : {', '.join(policy.protected_paths) or '(none)'}")
    for decision in decisions:
        verdict = "allow" if decision["allowed"] else "DENY "
        print(f"  {verdict} {decision['operation']:<20} [{decision['rule']}]")
    return EXIT_OK


# ======================================================================================
# Parser
# ======================================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awayctl",
        description=(
            "Deterministic controller for Claude Away (pre-alpha). "
            "Owns task state, evidence and leases; deliberately provides no way to set a "
            "task status by hand."
        ),
    )
    parser.add_argument("--db", help="path to the state database", default=None)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print version information").set_defaults(func=cmd_version)
    sub.add_parser("init", help="create and migrate the state database").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="validate persisted state").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="summarise the plan").set_defaults(func=cmd_status)

    gate = sub.add_parser("gate", help="explain a task's evidence gate")
    gate.add_argument("task_id")
    gate.set_defaults(func=cmd_gate)

    validate = sub.add_parser("validate-config", help="validate a configuration file")
    validate.add_argument("path")
    validate.set_defaults(func=cmd_validate_config)

    repos = sub.add_parser("repos", help="inspect enrolled repositories (read-only)")
    repos.add_argument("--config", required=True, help="path to the configuration file")
    repos.set_defaults(func=cmd_repos)

    policy = sub.add_parser("policy", help="show the allow/deny matrix for a configuration")
    policy.add_argument("--config", required=True, help="path to the configuration file")
    policy.set_defaults(func=cmd_policy)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code: int = args.func(args)
        return exit_code
    except ClaudeAwayError as error:
        if args.json:
            print(json.dumps(error.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(f"error [{error.code}]: {error}", file=sys.stderr)
        return EXIT_DOMAIN
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
