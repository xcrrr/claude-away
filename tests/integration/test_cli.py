"""``awayctl``: exit codes, JSON output, and the commands that deliberately do not exist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
        }
