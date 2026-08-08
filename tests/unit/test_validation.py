"""Schema contracts and the cross-object invariants JSON Schema cannot express."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from claude_away.core.validation import (
    config_schema,
    task_proposal_schema,
    task_schema,
    validate_config_document,
    validate_task_collection,
    validate_task_document,
    validate_task_proposal,
)
from claude_away.errors import (
    DependencyCycleError,
    MissingDependencyError,
    PlanVersionError,
    SchemaValidationError,
    ValidationError,
)
from tests.conftest import config_document, task_document


class TestSchemasThemselves:
    @pytest.mark.parametrize("loader", [task_schema, config_schema, task_proposal_schema])
    def test_schema_is_valid_draft_2020_12(self, loader: Any) -> None:
        Draft202012Validator.check_schema(loader())


class TestTaskDocument:
    def test_valid_document_passes(self) -> None:
        validate_task_document(task_document("AWAY-0001"))

    @pytest.mark.parametrize(
        "field",
        [
            "id",
            "goalIds",
            "projectId",
            "title",
            "estimatedEffort",
            "verification",
            "status",
            "createdByPlanVersion",
            "updatedByPlanVersion",
            "createdAt",
        ],
    )
    def test_required_fields_are_required(self, field: str) -> None:
        document = task_document("AWAY-0001")
        del document[field]
        with pytest.raises(SchemaValidationError):
            validate_task_document(document)

    @pytest.mark.parametrize(
        "task_id,valid",
        [
            ("AWAY-0001", True),
            ("AWAY-9999", True),
            ("AWAY-10000", True),
            ("AWAY-001", False),
            ("AWAY-00001", False),  # a second spelling of AWAY-0001
            ("away-0001", False),
            ("AWAY-abcd", False),
        ],
    )
    def test_task_id_pattern_admits_one_spelling_per_id(self, task_id: str, valid: bool) -> None:
        document = task_document("AWAY-0002")
        document["id"] = task_id
        if valid:
            validate_task_document(document)
        else:
            with pytest.raises(SchemaValidationError):
                validate_task_document(document)

    def test_unknown_property_is_rejected(self) -> None:
        document = task_document("AWAY-0001")
        document["sneaky"] = True
        with pytest.raises(SchemaValidationError):
            validate_task_document(document)

    def test_plan_version_must_not_go_backwards(self) -> None:
        document = task_document("AWAY-0001")
        document["createdByPlanVersion"] = 5
        document["updatedByPlanVersion"] = 4
        with pytest.raises(PlanVersionError):
            validate_task_document(document)

    def test_self_dependency_is_rejected(self) -> None:
        document = task_document("AWAY-0001", dependencies=["AWAY-0001"])
        with pytest.raises(ValidationError):
            validate_task_document(document)


class TestVerificationContract:
    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "x", "type": "command", "required": True},
            {"id": "x", "type": "artifact", "required": True},
            {"id": "x", "type": "git", "required": True},
            {"id": "x", "type": "review", "required": True},
            {"id": "x", "type": "manual", "required": True},
        ],
    )
    def test_every_type_needs_its_payload(self, entry: dict[str, Any]) -> None:
        with pytest.raises(SchemaValidationError):
            validate_task_document(task_document("AWAY-0001", verification=[entry]))

    @pytest.mark.parametrize(
        "entry",
        [
            # A verifier dispatching on the presence of `command` would run `true` here
            # and record a pass for an artifact check that was never performed.
            {"id": "x", "type": "artifact", "required": True, "path": "d", "command": "true"},
            {"id": "x", "type": "command", "required": True, "command": "pytest", "path": "d"},
        ],
    )
    def test_cross_type_field_leakage_is_rejected(self, entry: dict[str, Any]) -> None:
        with pytest.raises(SchemaValidationError):
            validate_task_document(task_document("AWAY-0001", verification=[entry]))

    def test_duplicate_verification_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate verification"):
            validate_task_document(
                task_document(
                    "AWAY-0001",
                    verification=[
                        {"id": "dup", "type": "command", "required": True, "command": "a"},
                        {"id": "dup", "type": "command", "required": False, "command": "b"},
                    ],
                )
            )

    def test_a_task_with_no_required_verification_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no required verification"):
            validate_task_document(
                task_document(
                    "AWAY-0001",
                    verification=[
                        {"id": "lint", "type": "command", "required": False, "command": "ruff"}
                    ],
                )
            )

    def test_an_llm_review_cannot_be_the_only_gate(self) -> None:
        """STATE_MODEL: a deterministic check cannot be replaced by an LLM opinion."""
        with pytest.raises(ValidationError, match="only thing gating"):
            validate_task_document(
                task_document(
                    "AWAY-0001",
                    verification=[
                        {
                            "id": "review",
                            "type": "review",
                            "required": True,
                            "description": "looks fine?",
                        }
                    ],
                )
            )

    def test_review_alongside_a_deterministic_check_is_fine(self) -> None:
        validate_task_document(
            task_document(
                "AWAY-0001",
                verification=[
                    {"id": "tests", "type": "command", "required": True, "command": "pytest"},
                    {
                        "id": "review",
                        "type": "review",
                        "required": True,
                        "description": "architecture sanity",
                    },
                ],
            )
        )

    def test_duplicate_acceptance_criterion_ids_are_rejected(self) -> None:
        document = task_document("AWAY-0001")
        document["acceptanceCriteria"] = [
            {"id": "same", "text": "one"},
            {"id": "same", "text": "two"},
        ]
        with pytest.raises(ValidationError, match="duplicate acceptance"):
            validate_task_document(document)


class TestTaskProposal:
    def test_valid_proposal_passes(self) -> None:
        validate_task_proposal(
            {
                "goalIds": ["ship"],
                "projectId": "api",
                "title": "Do the thing",
                "description": "details",
                "dependencies": [],
                "priority": 50,
                "risk": "low",
                "estimatedEffort": "small",
                "acceptanceCriteria": [{"id": "c", "text": "works"}],
                "verification": [
                    {"id": "tests", "type": "command", "required": True, "command": "pytest"}
                ],
                "humanRequired": False,
            }
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("status", "DONE"),
            ("createdAt", "2026-01-01T00:00:00.000000+00:00"),
            ("createdByPlanVersion", 1),
            ("updatedByPlanVersion", 1),
            ("schemaVersion", 1),
        ],
    )
    def test_a_planner_cannot_assert_controller_owned_fields(self, field: str, value: Any) -> None:
        """ARCHITECTURE: 'a planner cannot directly mark tasks DONE' -- made structural.

        These fields are not declared on the proposal shape at all, so a model that emits
        ``"status": "DONE"`` is rejected before anything looks at its reasoning.
        """
        proposal: dict[str, Any] = {
            "goalIds": ["ship"],
            "projectId": "api",
            "title": "Do the thing",
            "description": "details",
            "dependencies": [],
            "priority": 50,
            "risk": "low",
            "estimatedEffort": "small",
            "acceptanceCriteria": [{"id": "c", "text": "works"}],
            "verification": [
                {"id": "tests", "type": "command", "required": True, "command": "pytest"}
            ],
            "humanRequired": False,
            field: value,
        }
        with pytest.raises(SchemaValidationError):
            validate_task_proposal(proposal)


class TestTaskCollection:
    def test_valid_collection_passes(self) -> None:
        validate_task_collection(
            [
                task_document("AWAY-0001"),
                task_document("AWAY-0002", dependencies=["AWAY-0001"]),
            ],
            known_project_ids=["api"],
            known_goal_ids=["ship"],
        )

    def test_duplicate_task_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate task ids"):
            validate_task_collection([task_document("AWAY-0001"), task_document("AWAY-0001")])

    def test_dangling_dependency_is_rejected(self) -> None:
        with pytest.raises(MissingDependencyError):
            validate_task_collection([task_document("AWAY-0001", dependencies=["AWAY-0404"])])

    def test_cycle_is_rejected(self) -> None:
        with pytest.raises(DependencyCycleError):
            validate_task_collection(
                [
                    task_document("AWAY-0001", dependencies=["AWAY-0002"]),
                    task_document("AWAY-0002", dependencies=["AWAY-0001"]),
                ]
            )

    def test_unknown_project_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown project"):
            validate_task_collection([task_document("AWAY-0001")], known_project_ids=["web"])

    def test_unknown_goal_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown goals"):
            validate_task_collection([task_document("AWAY-0001")], known_goal_ids=["other"])


class TestConfigDocument:
    def test_valid_config_passes(self) -> None:
        validate_config_document(config_document())

    def test_duplicate_project_ids_are_rejected(self) -> None:
        document = config_document()
        document["projects"] = [
            {"id": "api", "path": "/a"},
            {"id": "api", "path": "/b"},
        ]
        with pytest.raises(ValidationError, match="duplicate project"):
            validate_config_document(document)

    def test_duplicate_goal_ids_are_rejected(self) -> None:
        document = config_document()
        document["goals"] = [
            {"id": "ship", "title": "A", "priority": 1, "successCriteria": ["x"]},
            {"id": "ship", "title": "B", "priority": 2, "successCriteria": ["y"]},
        ]
        with pytest.raises(ValidationError, match="duplicate goal"):
            validate_config_document(document)

    def test_local_project_requires_a_path(self) -> None:
        document = config_document()
        document["projects"] = [{"id": "api"}]
        with pytest.raises(SchemaValidationError):
            validate_config_document(document)

    def test_cloud_project_requires_a_repository(self) -> None:
        document = config_document(mode="cloud")
        document["projects"] = [{"id": "api", "path": "/a"}]
        with pytest.raises(SchemaValidationError):
            validate_config_document(document)

    def test_hybrid_mode_is_no_longer_accepted(self) -> None:
        with pytest.raises(SchemaValidationError):
            validate_config_document(config_document(mode="hybrid"))

    def test_disabled_brain_cannot_require_graphify(self) -> None:
        document = config_document()
        document["brain"] = {
            "enabled": False,
            "root": ".claude-away/brain",
            "obsidian": True,
            "graphify": "required",
        }
        with pytest.raises(SchemaValidationError):
            validate_config_document(document)

    def test_heartbeat_must_be_well_under_the_lease(self) -> None:
        document = config_document()
        document["execution"] = {
            "maxAttemptsPerTask": 3,
            "maxConcurrentTasks": 1,
            "leaseSeconds": 60,
            "leaseHeartbeatSeconds": 50,
        }
        with pytest.raises(ValidationError, match="half of leaseSeconds"):
            validate_config_document(document)

    def test_busywork_cannot_be_enabled(self) -> None:
        """`noBusywork` is a const, not a boolean. Refusing to farm tokens is not a toggle."""
        document = config_document()
        document["capacity"]["noBusywork"] = False
        with pytest.raises(SchemaValidationError):
            validate_config_document(document)
