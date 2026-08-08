"""Deterministic task-graph validation and readiness.

Two responsibilities:

* **Validation** -- reject a graph that could not be executed safely: dangling
  dependencies, self-dependencies, duplicates, cycles.
* **Readiness** -- decide, from status alone, which tasks are eligible to run.

Everything here is pure: it operates on plain mappings, never touches the database, and
returns the same answer for the same input regardless of iteration order. That is what
makes it exhaustively testable, and it is why the cycle diagnostic can afford to report
the actual cycle path rather than just asserting that one exists.

The traversals are iterative. A recursive depth-first search is the natural way to write
cycle detection and the wrong way to ship it: a deep dependency chain built up over an
eight-day autonomous run would hit Python's recursion limit and take the supervisor down
with it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from claude_away.core.models import TaskStatus
from claude_away.errors import (
    DependencyCycleError,
    DuplicateDependencyError,
    MissingDependencyError,
    SelfDependencyError,
)

__all__ = [
    "DependencyReport",
    "TaskNode",
    "blocking_dependencies",
    "compute_ready",
    "find_cycle",
    "topological_order",
    "validate_graph",
]


@dataclass(frozen=True, slots=True)
class TaskNode:
    """The minimum a task must expose for graph reasoning."""

    id: str
    status: TaskStatus
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DependencyReport:
    """Why a specific task is or is not ready."""

    task_id: str
    unsatisfied: tuple[str, ...]
    """Dependencies that are not ``DONE``, sorted for stable output."""

    unsatisfiable: tuple[str, ...]
    """Dependencies that can never become ``DONE`` (``CANCELLED`` or ``FAILED``)."""

    @property
    def is_satisfied(self) -> bool:
        return not self.unsatisfied

    @property
    def is_permanently_blocked(self) -> bool:
        """True when at least one dependency is in an absorbing non-``DONE`` state.

        ``FAILED`` is included even though it is technically recoverable: a failed
        dependency cannot satisfy a downstream task until somebody re-plans it, so
        surfacing it as unsatisfiable is the honest signal for a human or a replan.
        """
        return bool(self.unsatisfiable)


def _as_nodes(tasks: Iterable[TaskNode] | Mapping[str, TaskNode]) -> dict[str, TaskNode]:
    if isinstance(tasks, Mapping):
        return dict(tasks)
    return {node.id: node for node in tasks}


def validate_graph(
    tasks: Iterable[TaskNode] | Mapping[str, TaskNode],
    *,
    raw_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Validate the whole graph, raising the first structural problem found.

    Checks run in a fixed order -- self-dependency, duplicates, missing, cycles -- so that
    a graph with several problems always reports the same one first. Deterministic
    diagnostics matter when the consumer is an unattended supervisor comparing runs.

    ``raw_dependencies`` lets the caller pass the pre-deduplication dependency lists.
    :class:`TaskNode` holds a tuple that may already have had duplicates collapsed (for
    example by a set-backed loader), and duplicate detection needs to see the original.
    """
    nodes = _as_nodes(tasks)

    for task_id in sorted(nodes):
        declared: Sequence[str] = (
            raw_dependencies.get(task_id, nodes[task_id].dependencies)
            if raw_dependencies is not None
            else nodes[task_id].dependencies
        )
        if task_id in declared:
            raise SelfDependencyError(task_id)

        seen: set[str] = set()
        duplicated: set[str] = set()
        for dependency in declared:
            if dependency in seen:
                duplicated.add(dependency)
            seen.add(dependency)
        if duplicated:
            raise DuplicateDependencyError(task_id, sorted(duplicated))

        missing = sorted(d for d in declared if d not in nodes)
        if missing:
            raise MissingDependencyError(task_id, missing)

    cycle = find_cycle(nodes)
    if cycle is not None:
        raise DependencyCycleError(cycle)


def find_cycle(
    tasks: Iterable[TaskNode] | Mapping[str, TaskNode],
) -> list[str] | None:
    """Return a concrete cycle path, or ``None`` if the graph is acyclic.

    The returned path is closed -- the first node is repeated as the last -- so a caller
    can print it directly, e.g. ``['AWAY-0001', 'AWAY-0002', 'AWAY-0001']``.

    Implemented as an explicit-stack depth-first search with three-colour marking. Nodes
    are visited in sorted order and each node's edges in sorted order, which makes the
    reported cycle deterministic when a graph contains more than one.
    """
    nodes = _as_nodes(tasks)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(nodes, WHITE)
    parent: dict[str, str | None] = dict.fromkeys(nodes)

    for root in sorted(nodes):
        if colour[root] != WHITE:
            continue

        # Each stack frame is (node, iterator over its remaining edges).
        colour[root] = GREY
        parent[root] = None
        stack: list[tuple[str, list[str], int]] = [
            (root, sorted(d for d in nodes[root].dependencies if d in nodes), 0)
        ]

        while stack:
            node, edges, index = stack[-1]
            if index >= len(edges):
                colour[node] = BLACK
                stack.pop()
                continue

            stack[-1] = (node, edges, index + 1)
            neighbour = edges[index]

            if colour[neighbour] == GREY:
                # Walk the parent chain back from `node` to `neighbour` to recover the
                # actual cycle rather than reporting a bare boolean.
                path = [node]
                cursor: str | None = node
                while cursor is not None and cursor != neighbour:
                    cursor = parent[cursor]
                    if cursor is None:
                        break
                    path.append(cursor)
                path.reverse()
                if path[0] != neighbour:
                    path.insert(0, neighbour)
                path.append(neighbour)
                return path

            if colour[neighbour] == WHITE:
                colour[neighbour] = GREY
                parent[neighbour] = node
                stack.append(
                    (
                        neighbour,
                        sorted(d for d in nodes[neighbour].dependencies if d in nodes),
                        0,
                    )
                )

    return None


def topological_order(
    tasks: Iterable[TaskNode] | Mapping[str, TaskNode],
) -> list[str]:
    """Return task ids in dependency-first order.

    Ties are broken by task id so the ordering is fully deterministic. Raises
    :class:`DependencyCycleError` if the graph is cyclic.
    """
    nodes = _as_nodes(tasks)
    indegree: dict[str, int] = dict.fromkeys(nodes, 0)
    dependents: dict[str, list[str]] = {task_id: [] for task_id in nodes}

    for task_id, node in nodes.items():
        for dependency in node.dependencies:
            if dependency in nodes:
                indegree[task_id] += 1
                dependents[dependency].append(task_id)

    # A sorted list used as a small priority queue: graphs here are human-scale, and the
    # determinism is worth more than the asymptotics.
    frontier = sorted(t for t, degree in indegree.items() if degree == 0)
    order: list[str] = []

    while frontier:
        task_id = frontier.pop(0)
        order.append(task_id)
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                frontier.append(dependent)
        frontier.sort()

    if len(order) != len(nodes):
        cycle = find_cycle(nodes)
        raise DependencyCycleError(cycle or sorted(set(nodes) - set(order)))

    return order


def blocking_dependencies(
    task_id: str, tasks: Iterable[TaskNode] | Mapping[str, TaskNode]
) -> DependencyReport:
    """Explain the dependency situation for one task.

    Only ``DONE`` satisfies a dependency. ``CANCELLED`` and ``FAILED`` are additionally
    reported as *unsatisfiable*, because treating a cancelled dependency as "just not done
    yet" would leave a task waiting forever with no signal to the user.
    """
    nodes = _as_nodes(tasks)
    node = nodes[task_id]

    unsatisfied: list[str] = []
    unsatisfiable: list[str] = []

    for dependency in sorted(set(node.dependencies)):
        dependency_node = nodes.get(dependency)
        if dependency_node is None:
            # A missing dependency is a validation failure, but readiness must still be
            # answerable without raising: report it as blocking rather than crashing a
            # status query.
            unsatisfied.append(dependency)
            unsatisfiable.append(dependency)
            continue
        if dependency_node.status is TaskStatus.DONE:
            continue
        unsatisfied.append(dependency)
        if dependency_node.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
            unsatisfiable.append(dependency)

    return DependencyReport(
        task_id=task_id,
        unsatisfied=tuple(unsatisfied),
        unsatisfiable=tuple(unsatisfiable),
    )


def compute_ready(
    tasks: Iterable[TaskNode] | Mapping[str, TaskNode],
) -> list[str]:
    """Return the ids of ``PENDING`` tasks whose dependencies are all ``DONE``.

    Deliberately narrow. This answers "which tasks *became* eligible", not "which tasks
    should run next" -- prioritisation is the scheduler's job in a later milestone, and
    conflating the two here would bake scheduling policy into the state core.

    Tasks already past ``PENDING`` are not returned: promoting a ``RUNNING`` task to
    ``READY`` would be a regression, and re-promoting a ``BLOCKED`` task must go through
    explicit blocker resolution so the reason is recorded.
    """
    nodes = _as_nodes(tasks)
    ready: list[str] = []
    for task_id in sorted(nodes):
        if nodes[task_id].status is not TaskStatus.PENDING:
            continue
        if blocking_dependencies(task_id, nodes).is_satisfied:
            ready.append(task_id)
    return ready
