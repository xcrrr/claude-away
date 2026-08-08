"""DAG validation and readiness."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claude_away.core.dag import (
    TaskNode,
    blocking_dependencies,
    compute_ready,
    find_cycle,
    topological_order,
    validate_graph,
)
from claude_away.core.models import TaskStatus
from claude_away.errors import (
    DependencyCycleError,
    DuplicateDependencyError,
    MissingDependencyError,
    SelfDependencyError,
)


def node(task_id: str, status: TaskStatus = TaskStatus.PENDING, *deps: str) -> TaskNode:
    return TaskNode(id=task_id, status=status, dependencies=tuple(deps))


class TestValidation:
    def test_valid_graph_passes(self) -> None:
        validate_graph(
            [
                node("AWAY-0001"),
                node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                node("AWAY-0003", TaskStatus.PENDING, "AWAY-0001", "AWAY-0002"),
            ]
        )

    def test_empty_graph_is_valid(self) -> None:
        validate_graph([])

    def test_missing_dependency_is_rejected(self) -> None:
        with pytest.raises(MissingDependencyError) as caught:
            validate_graph([node("AWAY-0001", TaskStatus.PENDING, "AWAY-0999")])
        assert caught.value.details["missing"] == ["AWAY-0999"]
        assert caught.value.code == "missing_dependency"

    def test_self_dependency_is_rejected(self) -> None:
        with pytest.raises(SelfDependencyError):
            validate_graph([node("AWAY-0001", TaskStatus.PENDING, "AWAY-0001")])

    def test_duplicate_dependency_is_rejected(self) -> None:
        with pytest.raises(DuplicateDependencyError) as caught:
            validate_graph(
                {
                    "AWAY-0001": node("AWAY-0001"),
                    "AWAY-0002": node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                },
                raw_dependencies={"AWAY-0001": [], "AWAY-0002": ["AWAY-0001", "AWAY-0001"]},
            )
        assert caught.value.details["duplicated"] == ["AWAY-0001"]


class TestCycles:
    def test_two_node_cycle_reports_the_path(self) -> None:
        with pytest.raises(DependencyCycleError) as caught:
            validate_graph(
                [
                    node("AWAY-0001", TaskStatus.PENDING, "AWAY-0002"),
                    node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                ]
            )
        cycle = caught.value.cycle
        # The diagnostic must name the actual cycle, closed, not merely assert one exists.
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"AWAY-0001", "AWAY-0002"}

    def test_multi_node_cycle_reports_the_path(self) -> None:
        with pytest.raises(DependencyCycleError) as caught:
            validate_graph(
                [
                    node("AWAY-0001", TaskStatus.PENDING, "AWAY-0003"),
                    node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                    node("AWAY-0003", TaskStatus.PENDING, "AWAY-0002"),
                ]
            )
        cycle = caught.value.cycle
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"AWAY-0001", "AWAY-0002", "AWAY-0003"}

    def test_self_loop_found_by_find_cycle(self) -> None:
        assert find_cycle([node("AWAY-0001", TaskStatus.PENDING, "AWAY-0001")]) is not None

    def test_acyclic_graph_returns_none(self) -> None:
        assert (
            find_cycle([node("AWAY-0001"), node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001")])
            is None
        )

    def test_diamond_is_not_a_cycle(self) -> None:
        assert (
            find_cycle(
                [
                    node("AWAY-0001"),
                    node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                    node("AWAY-0003", TaskStatus.PENDING, "AWAY-0001"),
                    node("AWAY-0004", TaskStatus.PENDING, "AWAY-0002", "AWAY-0003"),
                ]
            )
            is None
        )

    def test_deep_chain_does_not_hit_the_recursion_limit(self) -> None:
        # An eight-day run can accumulate a long dependency chain. A recursive DFS would
        # raise RecursionError here and take the supervisor down with it.
        depth = 5_000
        nodes = [node("AWAY-0001")]
        for index in range(2, depth + 1):
            nodes.append(node(f"AWAY-{index:04d}", TaskStatus.PENDING, f"AWAY-{index - 1:04d}"))
        assert find_cycle(nodes) is None
        assert len(topological_order(nodes)) == depth

    def test_cycle_detection_is_deterministic(self) -> None:
        nodes = [
            node("AWAY-0001", TaskStatus.PENDING, "AWAY-0002"),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
            node("AWAY-0003", TaskStatus.PENDING, "AWAY-0004"),
            node("AWAY-0004", TaskStatus.PENDING, "AWAY-0003"),
        ]
        results = {tuple(find_cycle(nodes) or ()) for _ in range(20)}
        assert len(results) == 1, "cycle diagnostics must not vary between runs"


class TestTopologicalOrder:
    def test_dependencies_come_first(self) -> None:
        order = topological_order(
            [
                node("AWAY-0003", TaskStatus.PENDING, "AWAY-0002"),
                node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                node("AWAY-0001"),
            ]
        )
        assert order == ["AWAY-0001", "AWAY-0002", "AWAY-0003"]

    def test_cycle_raises(self) -> None:
        with pytest.raises(DependencyCycleError):
            topological_order(
                [
                    node("AWAY-0001", TaskStatus.PENDING, "AWAY-0002"),
                    node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
                ]
            )


class TestReadiness:
    def test_task_without_dependencies_is_ready(self) -> None:
        assert compute_ready([node("AWAY-0001")]) == ["AWAY-0001"]

    def test_dependency_must_be_done(self) -> None:
        nodes = [
            node("AWAY-0001", TaskStatus.RUNNING),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
        ]
        assert compute_ready(nodes) == []

    def test_done_dependency_unblocks(self) -> None:
        nodes = [
            node("AWAY-0001", TaskStatus.DONE),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
        ]
        assert compute_ready(nodes) == ["AWAY-0002"]

    @pytest.mark.parametrize(
        "status", [TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.BLOCKED]
    )
    def test_non_done_dependency_never_satisfies(self, status: TaskStatus) -> None:
        nodes = [
            node("AWAY-0001", status),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
        ]
        assert compute_ready(nodes) == []

    @pytest.mark.parametrize("status", [TaskStatus.CANCELLED, TaskStatus.FAILED])
    def test_absorbing_dependency_is_reported_unsatisfiable(self, status: TaskStatus) -> None:
        nodes = [
            node("AWAY-0001", status),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
        ]
        report = blocking_dependencies("AWAY-0002", nodes)
        assert report.is_permanently_blocked
        assert report.unsatisfiable == ("AWAY-0001",)

    def test_blocked_dependency_is_unsatisfied_but_not_unsatisfiable(self) -> None:
        # BLOCKED is recoverable, so it must not be reported as a dead end.
        nodes = [
            node("AWAY-0001", TaskStatus.BLOCKED),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
        ]
        report = blocking_dependencies("AWAY-0002", nodes)
        assert report.unsatisfied == ("AWAY-0001",)
        assert not report.is_permanently_blocked

    def test_only_pending_tasks_are_promoted(self) -> None:
        # Promoting a RUNNING task back to READY would be a regression, and re-promoting a
        # BLOCKED task must go through explicit blocker resolution so a reason is recorded.
        for status in (TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.BLOCKED):
            assert compute_ready([node("AWAY-0001", status)]) == []

    def test_partial_satisfaction_does_not_promote(self) -> None:
        nodes = [
            node("AWAY-0001", TaskStatus.DONE),
            node("AWAY-0002", TaskStatus.PENDING),
            node("AWAY-0003", TaskStatus.PENDING, "AWAY-0001", "AWAY-0002"),
        ]
        assert compute_ready(nodes) == ["AWAY-0002"]

    def test_readiness_is_order_independent(self) -> None:
        nodes = [
            node("AWAY-0001", TaskStatus.DONE),
            node("AWAY-0002", TaskStatus.PENDING, "AWAY-0001"),
            node("AWAY-0003", TaskStatus.PENDING, "AWAY-0002"),
        ]
        assert compute_ready(nodes) == compute_ready(list(reversed(nodes)))


class TestProperties:
    @settings(max_examples=200, deadline=None)
    @given(
        edges=st.lists(st.tuples(st.integers(1, 8), st.integers(1, 8)), max_size=20, unique=True)
    )
    def test_topological_order_exists_exactly_when_acyclic(
        self, edges: list[tuple[int, int]]
    ) -> None:
        """The two algorithms must never disagree about whether a graph is acyclic."""
        ids = [f"AWAY-{i:04d}" for i in range(1, 9)]
        dependencies: dict[str, list[str]] = {task_id: [] for task_id in ids}
        for source, target in edges:
            if source != target:
                dependencies[f"AWAY-{source:04d}"].append(f"AWAY-{target:04d}")

        nodes = [
            TaskNode(id=task_id, status=TaskStatus.PENDING, dependencies=tuple(deps))
            for task_id, deps in dependencies.items()
        ]
        cycle = find_cycle(nodes)
        if cycle is None:
            order = topological_order(nodes)
            assert len(order) == len(ids)
            position = {task_id: index for index, task_id in enumerate(order)}
            for task_id, deps in dependencies.items():
                for dependency in deps:
                    assert position[dependency] < position[task_id]
        else:
            assert cycle[0] == cycle[-1]
            with pytest.raises(DependencyCycleError):
                topological_order(nodes)

    @settings(max_examples=100, deadline=None)
    @given(count=st.integers(min_value=0, max_value=30))
    def test_chain_promotes_exactly_one_task(self, count: int) -> None:
        """In a linear chain with nothing done, only the head is ever ready."""
        nodes = [
            TaskNode(
                id=f"AWAY-{i:04d}",
                status=TaskStatus.PENDING,
                dependencies=() if i == 1 else (f"AWAY-{i - 1:04d}",),
            )
            for i in range(1, count + 1)
        ]
        assert compute_ready(nodes) == (["AWAY-0001"] if count else [])
