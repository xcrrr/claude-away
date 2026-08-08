"""``awayctl``: exit codes, JSON output, and the commands that deliberately do not exist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from claude_away.cli.main import EXIT_DOMAIN, EXIT_OK, build_parser, main
from claude_away.core import repository as repo
from claude_away.core import state
from claude_away.core.db import open_database
from claude_away.core.evidence import record_evidence
from claude_away.core.leases import acquire_lease
from claude_away.core.models import EvidenceResult, EvidenceType
from tests.conftest import config_document, task_document

OWNER = "runner-test"


def seed(path: Path) -> None:
    db = open_database(path)
    try:
        repo.create_project(db, "api", path="/tmp/api")
        repo.create_goal(db, "ship", title="Ship", priority=90, success_criteria=["green"])
        repo.create_plan_version(db, reason="initial")
        repo.create_task(db, task_document("AWAY-0001"))
    finally:
        db.close()


class TestVersion:
    def test_human_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["version"]) == EXIT_OK
        assert "claude-away" in capsys.readouterr().out

    def test_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--json", "version"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] >= 1


class TestInit:
    def test_creates_a_database(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "state.db"
        assert main(["--json", "--db", str(path), "init"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["created"] is True
        assert path.exists()
        assert payload["runner_id"].startswith("runner-")

    def test_is_idempotent_and_keeps_the_runner_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "state.db"
        main(["--json", "--db", str(path), "init"])
        first = json.loads(capsys.readouterr().out)["runner_id"]
        assert main(["--json", "--db", str(path), "init"]) == EXIT_OK
        second = json.loads(capsys.readouterr().out)
        assert second["created"] is False
        assert second["runner_id"] == first

    def test_database_is_owner_only(self, tmp_path: Path) -> None:
        """The state file is the authority for a multi-day run; keep it 0600."""
        path = tmp_path / "state.db"
        main(["--db", str(path), "init"])
        assert path.stat().st_mode & 0o077 == 0


class TestStatus:
    def test_requires_an_existing_database(self, tmp_path: Path) -> None:
        assert main(["--db", str(tmp_path / "missing.db"), "status"]) == EXIT_DOMAIN

    def test_json_lists_tasks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "state.db"
        seed(path)
        assert main(["--json", "--db", str(path), "status"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["PENDING"] == 1
        assert payload["tasks"][0]["id"] == "AWAY-0001"


class TestDoctor:
    def test_healthy_state(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "state.db"
        seed(path)
        assert main(["--json", "--db", str(path), "doctor"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["healthy"] is True

    def test_reports_expired_leases(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "state.db"
        seed(path)
        db = open_database(path)
        try:
            acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=3600)
            # Backdate the expiry rather than sleeping. The CLI uses a real SystemClock,
            # so waiting for a short lease to lapse would make this test both slow and
            # flaky; rewriting the timestamp makes it neither.
            db.execute(
                "UPDATE leases SET expires_at = '2020-01-01T00:00:00.000000+00:00' "
                "WHERE task_id = 'AWAY-0001'"
            )
        finally:
            db.close()

        assert main(["--json", "--db", str(path), "doctor"]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)
        kinds = {problem["kind"] for problem in payload["problems"]}
        assert "expired_lease_requires_reconciliation" in kinds

    def test_reports_missing_triggers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "state.db"
        seed(path)
        db = open_database(path)
        try:
            db.connection.execute("DROP TRIGGER tasks_status_requires_transition_layer")
        finally:
            db.close()
        assert main(["--json", "--db", str(path), "doctor"]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)
        assert any(p["kind"] == "missing_integrity_triggers" for p in payload["problems"])


class TestGate:
    def test_closed_gate_exits_non_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A future TaskCompleted hook shells this; the exit code is the contract."""
        path = tmp_path / "state.db"
        seed(path)
        db = open_database(path)
        try:
            state.refresh_readiness(db)
            acquire_lease(db, "AWAY-0001", OWNER, duration_seconds=3600)
            state.start_attempt(db, "AWAY-0001", owner_id=OWNER)
            state.begin_verification(db, "AWAY-0001", owner_id=OWNER)
        finally:
            db.close()

        assert main(["--json", "--db", str(path), "gate", "AWAY-0001"]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)
        assert payload["satisfied"] is False
        assert payload["missing"] == ["unit-tests"]

    def test_open_gate_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "state.db"
        seed(path)
        db = open_database(path)
        try:
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
        finally:
            db.close()

        assert main(["--json", "--db", str(path), "gate", "AWAY-0001"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["satisfied"] is True


class TestValidateConfig:
    def test_valid_config(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config_document()), encoding="utf-8")
        assert main(["--db", str(tmp_path / "s.db"), "validate-config", str(path)]) == EXIT_OK

    def test_invalid_config_reports_a_domain_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config_document(mode="hybrid")), encoding="utf-8")
        assert (
            main(["--json", "--db", str(tmp_path / "s.db"), "validate-config", str(path)])
            == EXIT_DOMAIN
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "schema_validation_error"


class TestNoUnsafeCommands:
    def test_there_is_no_way_to_set_a_task_status(self) -> None:
        """Convenient during development, and a total bypass of the evidence gate.

        If this test ever fails because someone added such a verb, that is the signal to
        have the argument -- not to update the test.
        """
        parser = build_parser()
        commands: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands.update(action.choices)

        forbidden = {"set-status", "set_status", "complete", "done", "force-done", "transition"}
        assert not (commands & forbidden), f"unsafe command exposed: {commands & forbidden}"
        assert commands == {
            "version",
            "init",
            "doctor",
            "status",
            "gate",
            "validate-config",
            "repos",
            "policy",
        }


class TestRepoAndPolicyCommands:
    """The Milestone 2A surface. Read-only: neither command may touch a repository."""

    def _config(self, tmp_path: Path, *, dirty: bool = False) -> Path:
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api")
        if dirty:
            repo.write("scratch.txt", "x")

        document = config_document()
        document["projects"] = [{"id": "api", "path": str(repo.path), "defaultBranch": "main"}]
        document["stateDbPath"] = str(tmp_path / "state" / "state.db")
        document["safety"]["protectedPaths"] = ["infra"]

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")
        return config_path

    def test_repos_reports_a_ready_repository(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = self._config(tmp_path)
        assert main(["--json", "repos", "--config", str(config_path)]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 1
        assert payload["ready"] == 1
        assert payload["repositories"][0]["base"]["resolved"] is True

    def test_repos_exits_non_zero_when_a_repository_is_not_ready(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A blocked repository is a real signal, so the exit code carries it."""
        config_path = self._config(tmp_path, dirty=True)
        assert main(["--json", "repos", "--config", str(config_path)]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)
        assert payload["ready"] == 0
        assert "dirty_worktree" in payload["repositories"][0]["base"]["refusals"]

    def test_repos_does_not_modify_the_repository(self, tmp_path: Path) -> None:
        from tests.gitfixtures import GitRepo

        config_path = self._config(tmp_path, dirty=True)
        repo = GitRepo(tmp_path / "api")
        before_status = repo.git("status", "--porcelain=v2", "-z").stdout
        before_head = repo.head()

        main(["--json", "repos", "--config", str(config_path)])

        assert repo.git("status", "--porcelain=v2", "-z").stdout == before_status
        assert repo.head() == before_head

    def test_repos_refuses_an_unenrolled_state_db_location(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api")
        document = config_document()
        document["projects"] = [{"id": "api", "path": str(repo.path)}]
        document["stateDbPath"] = str(repo.path / "state.db")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")

        assert main(["--json", "repos", "--config", str(config_path)]) == EXIT_DOMAIN
        assert json.loads(capsys.readouterr().out)["code"] == "unsafe_state_location"

    def test_policy_prints_the_full_matrix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from claude_away.core.policy import Operation

        config_path = self._config(tmp_path)
        assert main(["--json", "policy", "--config", str(config_path)]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)

        assert {d["operation"] for d in payload["decisions"]} == {op.value for op in Operation}
        assert payload["protected_branches"] == ["main"]
        by_operation = {d["operation"]: d for d in payload["decisions"]}
        assert by_operation["force_push"]["allowed"] is False
        assert by_operation["push"]["allowed"] is False
        assert by_operation["inspect"]["allowed"] is True

    def test_policy_needs_no_repository_access(self, tmp_path: Path) -> None:
        """Pure evaluation: it must work even if the repository has since vanished."""
        import shutil

        config_path = self._config(tmp_path)
        shutil.rmtree(tmp_path / "api")
        assert main(["--json", "policy", "--config", str(config_path)]) == EXIT_OK

    @pytest.mark.parametrize("command", ["repos", "policy"])
    def test_the_config_path_may_be_positional_like_validate_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
    ) -> None:
        """`validate-config <path>` works, so `repos <path>` must too.

        Otherwise the second command an operator types after validating their config is a
        usage error, for no reason other than which spelling each parser happened to get.
        """
        config_path = self._config(tmp_path)
        assert main(["--json", command, str(config_path)]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)

    @pytest.mark.parametrize("command", ["repos", "policy"])
    def test_omitting_the_config_path_is_a_domain_error_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
    ) -> None:
        assert main(["--json", command]) == EXIT_DOMAIN
        assert json.loads(capsys.readouterr().out)["code"] == "validation_error"

    @pytest.mark.parametrize("command", ["repos", "policy"])
    def test_two_conflicting_config_paths_are_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
    ) -> None:
        """Silently preferring one would run against a file the operator did not name."""
        config_path = self._config(tmp_path)
        other = tmp_path / "other.json"
        other.write_text("{}", encoding="utf-8")

        assert main(["--json", command, str(config_path), "--config", str(other)]) == EXIT_DOMAIN
        assert json.loads(capsys.readouterr().out)["code"] == "validation_error"

    @pytest.mark.parametrize("command", ["repos", "policy"])
    def test_the_same_path_given_twice_is_accepted(self, tmp_path: Path, command: str) -> None:
        config_path = self._config(tmp_path)
        assert main(["--json", command, str(config_path), "--config", str(config_path)]) == EXIT_OK


class TestReposIsolatesPerRepositoryFailures:
    def test_one_unreadable_repository_does_not_blind_the_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A supervisor reads this to decide what to work on.

        A single repository that cannot be inspected used to raise out of the loop and take
        every other repository's verdict with it -- and an unreadable repository is exactly
        the situation where knowing about the others matters.
        """
        from tests.gitfixtures import make_repo

        healthy = make_repo(tmp_path / "healthy")
        broken = make_repo(tmp_path / "broken")
        broken.git("config", "filter.evil.clean", "/bin/true")

        document = config_document()
        document["projects"] = [
            {"id": "broken", "path": str(broken.path), "defaultBranch": "main"},
            {"id": "healthy", "path": str(healthy.path), "defaultBranch": "main"},
        ]
        document["stateDbPath"] = str(tmp_path / "state" / "state.db")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")

        assert main(["--json", "repos", str(config_path)]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)

        by_id = {entry["project_id"]: entry for entry in payload["repositories"]}
        assert by_id["broken"]["error"]["code"] == "unsafe_repository_config"
        assert by_id["healthy"]["base"]["resolved"] is True
        assert payload["count"] == 2


class TestPolicyAccountsForDiscoveredBranches:
    def _config(self, tmp_path: Path, *, declare: bool) -> Path:
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api", initial_branch="trunk")
        repo.git("update-ref", "refs/remotes/origin/trunk", repo.head())
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        project: dict[str, Any] = {"id": "api", "path": str(repo.path)}
        if declare:
            project["defaultBranch"] = "trunk"

        document = config_document()
        document["projects"] = [project]
        document["stateDbPath"] = str(tmp_path / "state" / "state.db")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")
        return config_path

    def test_a_discovered_default_branch_is_protected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`repos` resolved a base on `trunk` while `policy` reported no protected branch."""
        config_path = self._config(tmp_path, declare=False)
        assert main(["--json", "policy", str(config_path)]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["protected_branches"] == ["trunk"]
        assert payload["projects_with_unknown_default_branch"] == []

    def test_a_declared_branch_is_still_protected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = self._config(tmp_path, declare=True)
        assert main(["--json", "policy", str(config_path)]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["protected_branches"] == ["trunk"]

    def test_a_project_with_no_determinable_default_is_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ "No protected branch" and "we could not tell" are different answers."""
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api", initial_branch="trunk")
        document = config_document()
        document["projects"] = [{"id": "api", "path": str(repo.path)}]
        document["stateDbPath"] = str(tmp_path / "state" / "state.db")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")

        assert main(["--json", "policy", str(config_path)]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["protected_branches"] == []
        assert payload["projects_with_unknown_default_branch"] == ["api"]

    def test_the_matrix_still_prints_when_the_repository_is_gone(self, tmp_path: Path) -> None:
        """The flags are a property of the configuration, not of the filesystem."""
        import shutil

        config_path = self._config(tmp_path, declare=True)
        shutil.rmtree(tmp_path / "api")
        assert main(["--json", "policy", str(config_path)]) == EXIT_OK


class TestInitRefusesToLiveInsideARepository:
    def test_the_default_path_inside_a_repository_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ledger that decides DONE must not sit inside the thing it judges.

        `enrol_projects` refuses a configured stateDbPath inside an enrolled repository,
        but `awayctl init` took its path from a working-directory-relative default that
        never went near that check.
        """
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api")
        monkeypatch.chdir(repo.path)

        assert main(["--json", "init"]) == EXIT_DOMAIN
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == "unsafe_state_location"
        assert not (repo.path / ".claude-away").exists()

    def test_an_explicit_db_path_inside_a_repository_is_also_refused(self, tmp_path: Path) -> None:
        from tests.gitfixtures import make_repo

        repo = make_repo(tmp_path / "api")
        assert main(["--json", "--db", str(repo.path / "nested" / "state.db"), "init"]) == (
            EXIT_DOMAIN
        )

    def test_a_path_outside_every_repository_still_works(self, tmp_path: Path) -> None:
        assert main(["--json", "--db", str(tmp_path / "state" / "state.db"), "init"]) == EXIT_OK
        assert (tmp_path / "state" / "state.db").exists()
