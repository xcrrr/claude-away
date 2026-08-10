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
from claude_away.errors import (
    ClaudeAwayError,
    GitError,
    NotAGitRepositoryError,
    UnsafeStateLocationError,
    ValidationError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DOMAIN = 3


def default_db_path() -> Path:
    """Where the state database lives when nothing says otherwise.

    Under the XDG state directory, not ``./.claude-away/state.db``. The working-directory
    relative default meant ``awayctl init`` run from inside a repository -- the obvious
    place to run it, and what the README's own quickstart does -- created the ledger that
    decides whether work is DONE *inside a repository Claude Away works in*: the exact
    arrangement ``enrol_projects`` refuses for a configured ``stateDbPath``. It also left an
    untracked directory behind, so every later inspection reported that repository dirty and
    base resolution refused forever.

    A path anchored to the user rather than to the shell's cwd cannot drift into a
    repository by accident, and one state database per user is the right default for a tool
    that supervises several repositories at once.
    """
    root = os.environ.get("XDG_STATE_HOME")
    # The XDG spec says a relative value is invalid and must be ignored. Honouring one would
    # reintroduce the exact property this function was rewritten to remove: a default that
    # resolves against whatever directory the shell happens to be in.
    base = Path(root) if root and Path(root).is_absolute() else Path.home() / ".local" / "state"
    return base / "claude-away" / "state.db"


def _emit(payload: Any, *, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(human)


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    env = os.environ.get("CLAUDE_AWAY_DB")
    return Path(env) if env else default_db_path()


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


def _assert_db_outside_a_repository(path: Path) -> None:
    """Refuse to put the state database inside a Git repository.

    ``enrol_projects`` already refuses a configured ``stateDbPath`` inside an enrolled
    repository, for a reason it states plainly: the ledger that decides whether work is DONE
    must not be reachable from inside the thing being judged. But ``awayctl init`` takes its
    path from ``--db``, ``CLAUDE_AWAY_DB`` or a *working-directory-relative* default, none of
    which went anywhere near that check -- so running ``awayctl init`` from inside a
    repository created exactly the arrangement the other guard exists to forbid, and left an
    untracked ``.claude-away/`` behind that made every later inspection report the repository
    dirty.

    Checked against any repository, not just enrolled ones: at ``init`` time there is no
    configuration to consult, and "inside some repository" is the property that matters.

    Only ``NotAGitRepositoryError`` means "not a repository". Every other Git failure means
    *we could not tell*, and this used to catch the base ``GitError`` and treat all of them
    as a pass -- so a repository carrying a repository-local command-bearing key, which is
    exactly what ``git lfs install --local`` writes, turned the guard off for itself. So did
    a Git older than 2.26, for every repository. That is the same fail-open shape the config
    audit had one layer down: a check that did not run is not a pass.
    """
    resolved = path.expanduser().resolve()
    # Nearest existing DIRECTORY, not nearest existing path. Stopping at the first thing
    # that exists meant a *file* in the chain ended the walk -- `--db <repo>/somefile/x/db`
    # probed `<repo>/somefile`, `git -C` on a file fails, and the guard read that as "not a
    # repository" and passed. The ledger did not actually land there (mkdir fails next), but
    # the guard was answering a different question than it appears to, and its answer was
    # the unsafe one.
    probe = resolved.parent
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent

    try:
        root: Path | None = inspect_repository(probe).root
    except NotAGitRepositoryError:
        return  # genuinely not a repository, which is what we want
    except GitError as exc:
        raise UnsafeStateLocationError(
            f"cannot determine whether {probe} is inside a Git repository, so refusing to "
            f"create the state database there. The ledger that decides whether work is DONE "
            f"must live outside the repositories it judges, and a check that could not run "
            f"is not a check that passed",
            path=str(path),
            probe=str(probe),
            reason=exc.code,
            detail=exc.message,
        ) from exc

    raise UnsafeStateLocationError(
        f"refusing to create the state database inside the Git repository at "
        f"{root}: the ledger that decides whether work is DONE must live "
        f"outside the repositories it judges. Pass --db, or set CLAUDE_AWAY_DB, to a "
        f"location outside any repository",
        path=str(path),
        repository_root=str(root),
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Create and migrate a state database."""
    path = _resolve_db_path(args)
    _assert_db_outside_a_repository(path)
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


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    """Accept the configuration path either positionally or as ``--config``.

    ``validate-config`` takes a bare path, so a command that only accepted ``--config``
    would make the very next thing an operator types an error. Both spellings work;
    supplying neither, or both, is refused rather than silently preferring one.
    """
    parser.add_argument("path", nargs="?", help="path to the configuration file")
    parser.add_argument("--config", dest="config", default=None, help=argparse.SUPPRESS)


def _config_path(args: argparse.Namespace) -> Path:
    positional, flag = args.path, args.config
    if positional and flag and Path(positional) != Path(flag):
        raise ValidationError(
            "give the configuration path once, not both positionally and --config"
        )
    chosen = positional or flag
    if not chosen:
        raise ValidationError("a configuration file path is required")
    return Path(chosen)


def cmd_repos(args: argparse.Namespace) -> int:
    """Inspect every enrolled repository. Strictly read-only.

    Exercises the whole Milestone 2A boundary end to end: enrolment canonicalises and
    authorises the paths, Git inspection describes them, and base resolution says whether
    each one could be branched from. Nothing here writes to a repository.
    """
    config_path = _config_path(args)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config_document(document)

    enrolment = enrol_projects(document, config_dir=config_path.parent)

    repositories: list[dict[str, Any]] = [
        {**failure.to_dict(), "root": str(failure.configured_path)}
        for failure in enrolment.failures
    ]
    ready = 0
    for repository in enrolment.repositories:
        entry: dict[str, Any] = dict(repository.to_dict())
        try:
            # The inspection enrolment already performed, not a second one. Re-inspecting
            # with `configured_default_branch=repository.default_branch` fed a value the
            # repository may itself have asserted back in through the parameter reserved for
            # the operator's declaration, so the JSON carried `origin_head` at the top level
            # and `configured` in the nested inspection for the same branch -- and the false
            # one claimed operator authority.
            inspection = enrolment.inspections[repository.project_id]
            resolution = resolve_expected_base(inspection, project_id=repository.project_id)
        except GitError as exc:
            # One repository's failure must not take the report down with it. A supervisor
            # reading `awayctl repos --json` to decide what to work on would otherwise be
            # blinded to every other repository by a single unreadable one -- and an
            # unreadable repository is exactly the situation where knowing about the others
            # matters. The failure is reported in place, against the project it belongs to.
            entry["error"] = exc.to_dict()
            repositories.append(entry)
            continue

        ready += 1 if resolution.resolved else 0
        entry["inspection"] = inspection.to_dict()
        entry["base"] = resolution.to_dict()
        repositories.append(entry)

    payload = {"count": len(repositories), "ready": ready, "repositories": repositories}
    if args.json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK if ready == len(repositories) else EXIT_DOMAIN

    for entry in repositories:
        if "error" in entry:
            print(f" ERROR   {entry['project_id']}  {entry['root']}")
            print(f"      {entry['error']['code']}: {entry['error']['message']}")
            continue
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


def _protected_branches(document: dict[str, Any], config_dir: Path) -> tuple[list[str], list[str]]:
    """Every branch that must be treated as protected, plus the projects nothing covers.

    The configured ``defaultBranch`` alone is not enough. It is optional in the schema, and
    the whole point of :func:`_resolve_default_branch` is to work out the default branch
    when the configuration is silent -- so a project relying on discovery used to be
    reported by ``awayctl policy`` as having *no* protected branch while ``awayctl repos``
    happily resolved a base on it. The safety matrix understating the granted authority is
    the wrong direction for the one command an operator runs to check exactly that.

    Discovered branches are included, but the union is deliberate rather than a
    replacement: ``origin/HEAD`` lives inside the repository, so treating discovery as
    authoritative would let a decoy written there move protection off the real default.
    Taking both means a repository can only ever *add* to what is protected.

    The second return value names projects whose default branch could not be determined at
    all. Those are reported rather than silently omitted -- "no protected branch" and "we
    could not tell" are different answers, and only one of them is safe to act on.
    """
    branches: set[str] = set()
    unknown: list[str] = []

    declared: set[str] = set()
    for project in document.get("projects", []):
        configured = project.get("defaultBranch")
        if configured:
            declared.add(str(configured))
    branches |= declared

    try:
        enrolment = enrol_projects(document, config_dir=config_dir)
    except ClaudeAwayError:
        # The matrix is still worth printing when the repositories cannot be read -- the
        # flags are a property of the configuration, not of the filesystem -- so a failure
        # here degrades to "configured branches only" instead of taking the command down.
        #
        # But it must degrade *audibly*. Returning an empty `unknown` here said "every
        # project's default branch is accounted for" when in fact none had been checked,
        # collapsing the distinction the function exists to preserve. Every project without
        # a declaration is unknown, because that is exactly what we failed to find out.
        undeclared = [
            str(project.get("id", "<unnamed>"))
            for project in document.get("projects", [])
            if not project.get("defaultBranch")
        ]
        return sorted(branches), sorted(undeclared)

    for repository in enrolment.repositories:
        # Both, not whichever one won. `_resolve_default_branch` returns the configured
        # value the moment there is one, so taking only `default_branch` made the set
        # {declared} or {discovered} and never both -- and for a project with no declaration
        # a decoy in `refs/remotes/origin/HEAD` therefore *moved* protection off the real
        # branch rather than adding to it, which is the opposite of what this documented.
        for candidate in (repository.default_branch, repository.discovered_default_branch):
            if candidate:
                branches.add(candidate)
        if not repository.default_branch:
            unknown.append(repository.project_id)

    return sorted(branches), sorted(unknown)


def cmd_policy(args: argparse.Namespace) -> int:
    """Print the full allow/deny matrix for a configuration.

    Reads repositories only to learn which branches are protected, and degrades to the
    configured branches if it cannot.
    """
    config_path = _config_path(args)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config_document(document)

    protected_branches, unknown_default = _protected_branches(document, config_path.parent)
    policy = SafetyPolicy.from_config(document, protected_branches=protected_branches)

    decisions = [policy.evaluate(operation).to_dict() for operation in Operation]
    payload = {
        "protected_branches": protected_branches,
        "protected_paths": list(policy.protected_paths),
        "projects_with_unknown_default_branch": unknown_default,
        "decisions": decisions,
    }
    if args.json:
        _emit(payload, as_json=True, human="")
        return EXIT_OK

    print(f"protected branches: {', '.join(protected_branches) or '(none)'}")
    print(f"protected paths   : {', '.join(policy.protected_paths) or '(none)'}")
    if unknown_default:
        print(f"default branch unknown for: {', '.join(unknown_default)}")
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

    # `path` positional, matching `validate-config`, so the three config-reading commands
    # take their argument the same way. `--config` stays as an accepted spelling because
    # it reads better in scripts, but exactly one of the two must be given.
    repos = sub.add_parser("repos", help="inspect enrolled repositories (read-only)")
    _add_config_argument(repos)
    repos.set_defaults(func=cmd_repos)

    policy = sub.add_parser("policy", help="show the allow/deny matrix for a configuration")
    _add_config_argument(policy)
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # UnicodeDecodeError is a ValueError, not an OSError, so a configuration file that is
        # UTF-16, carries the wrong BOM, or is simply not text produced a Python traceback
        # from every config-reading command rather than an error message and exit 1.
        message = {"code": "unreadable_file", "message": str(error)}
        if args.json:
            print(json.dumps(message, indent=2, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
