"""The implemented transition table must match ``docs/STATE_MODEL.md``.

Documentation that drifts from behaviour is worse than none: a contributor who reads the
table and trusts it will write code that the state machine rejects. This test parses the
Markdown table and compares it with the implementation, so any future edit to either one
has to be accompanied by an edit to the other.

Divergences are permitted but must be *declared* -- see ``BLOCKED_TO_PENDING_RATIONALE``
in :mod:`claude_away.core.models`.
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_away.core.models import (
    ALLOWED_TRANSITIONS,
    BLOCKED_TO_PENDING_RATIONALE,
    TaskStatus,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "STATE_MODEL.md"

# Edges implemented deliberately beyond the document's explicit rows. Each needs a reason.
DECLARED_DIVERGENCES: dict[tuple[TaskStatus, TaskStatus], str] = {
    (TaskStatus.BLOCKED, TaskStatus.PENDING): BLOCKED_TO_PENDING_RATIONALE,
}

_STATUS_NAMES = {status.value for status in TaskStatus}
_ANY_NON = re.compile(r"^any\s+non-(\w+)$", re.IGNORECASE)


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    # Backticks are Markdown emphasis, not part of the value, and they appear both around
    # a whole cell and inside the "any non-`DONE`" phrasing.
    return [cell.replace("`", "").strip() for cell in stripped.strip("|").split("|")]


def parse_documented_transitions() -> set[tuple[TaskStatus, TaskStatus]]:
    """Extract ``(from, to)`` pairs from the 'Allowed transitions' table."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Allowed transitions", 1)[1].split("\n## ", 1)[0]

    documented: set[tuple[TaskStatus, TaskStatus]] = set()
    for line in section.splitlines():
        cells = _cells(line)
        if len(cells) < 2:
            continue
        raw_from, raw_to = cells[0], cells[1]
        if raw_to not in _STATUS_NAMES:
            continue  # header row, separator row, or prose

        to_status = TaskStatus(raw_to)
        wildcard = _ANY_NON.match(raw_from)
        if wildcard:
            excluded = TaskStatus(wildcard.group(1).upper())
            for status in TaskStatus:
                if status is not excluded and status is not to_status:
                    documented.add((status, to_status))
            continue
        if raw_from in _STATUS_NAMES:
            documented.add((TaskStatus(raw_from), to_status))
    return documented


def implemented_transitions() -> set[tuple[TaskStatus, TaskStatus]]:
    return {
        (source, target) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets
    }


def test_document_parses() -> None:
    documented = parse_documented_transitions()
    assert documented, "failed to parse any transitions out of STATE_MODEL.md"
    assert (TaskStatus.PENDING, TaskStatus.READY) in documented
    assert (TaskStatus.VERIFYING, TaskStatus.DONE) in documented


def test_every_documented_transition_is_implemented() -> None:
    missing = parse_documented_transitions() - implemented_transitions()
    assert not missing, f"documented but not implemented: {sorted(map(str, missing))}"


def test_every_implemented_transition_is_documented_or_declared() -> None:
    extra = implemented_transitions() - parse_documented_transitions()
    undeclared = {edge for edge in extra if edge not in DECLARED_DIVERGENCES}
    assert not undeclared, (
        "implemented but neither documented nor declared as a deliberate divergence: "
        f"{sorted(map(str, undeclared))}"
    )


def test_declared_divergences_have_a_rationale() -> None:
    for edge, rationale in DECLARED_DIVERGENCES.items():
        assert rationale and len(rationale) > 40, f"{edge} needs a real explanation"


def test_done_is_absorbing() -> None:
    assert ALLOWED_TRANSITIONS[TaskStatus.DONE] == frozenset()


def test_cancelled_is_absorbing() -> None:
    assert ALLOWED_TRANSITIONS[TaskStatus.CANCELLED] == frozenset()


def test_cancellation_reachable_from_every_non_done_state() -> None:
    for status in TaskStatus:
        if status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            assert TaskStatus.CANCELLED not in ALLOWED_TRANSITIONS[status]
        else:
            assert TaskStatus.CANCELLED in ALLOWED_TRANSITIONS[status]
