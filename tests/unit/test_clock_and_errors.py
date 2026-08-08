"""The time source and the error taxonomy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_away.clock import ManualClock, SystemClock, ensure_utc, parse_timestamp, to_iso
from claude_away.errors import (
    ClaudeAwayError,
    DependencyCycleError,
    EvidenceIncompleteError,
    NotFoundError,
)


class TestClock:
    def test_system_clock_is_timezone_aware_utc(self) -> None:
        now = SystemClock().now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_manual_clock_only_moves_when_told(self) -> None:
        clock = ManualClock()
        first = clock.now()
        assert clock.now() == first
        clock.advance(seconds=5)
        assert clock.now() == first + timedelta(seconds=5)

    def test_manual_clock_refuses_to_go_backwards(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError):
            clock.advance(seconds=-1)
        with pytest.raises(ValueError):
            clock.set(clock.now() - timedelta(seconds=1))

    def test_naive_datetimes_are_rejected(self) -> None:
        """Guessing a timezone is how a scheduler silently drifts by hours."""
        with pytest.raises(ValueError):
            ensure_utc(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive

    def test_non_utc_is_normalised(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        moment = datetime(2026, 1, 1, 12, 0, tzinfo=eastern)
        assert ensure_utc(moment).hour == 17


class TestTimestampFormat:
    def test_round_trips(self) -> None:
        moment = datetime(2026, 3, 4, 5, 6, 7, 891234, tzinfo=timezone.utc)
        assert parse_timestamp(to_iso(moment)) == moment

    def test_is_fixed_width(self) -> None:
        """SQLite orders TEXT lexicographically, so width must not vary."""
        widths = {
            len(to_iso(datetime(2026, 1, 1, tzinfo=timezone.utc))),
            len(to_iso(datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc))),
            len(to_iso(datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc))),
        }
        assert len(widths) == 1

    def test_byte_order_matches_chronological_order(self) -> None:
        """The property the whole storage layer leans on when it sorts by timestamp."""
        moments = [
            datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 0, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc),
        ]
        rendered = [to_iso(moment) for moment in moments]
        assert rendered == sorted(rendered)

    def test_stored_timestamp_without_a_zone_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_timestamp("2026-01-01T00:00:00.000000")

    def test_matches_the_published_schema_pattern(self) -> None:
        import re

        from claude_away.core.validation import task_schema

        pattern = task_schema()["$defs"]["timestamp"]["pattern"]
        rendered = to_iso(datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc))
        assert re.match(pattern, rendered), f"{rendered!r} does not match {pattern!r}"


def _error_classes() -> list[type[ClaudeAwayError]]:
    """Every domain error defined in the module, found by introspection."""
    import inspect

    from claude_away import errors as error_module

    return [
        obj
        for _name, obj in inspect.getmembers(error_module, inspect.isclass)
        if issubclass(obj, ClaudeAwayError) and obj.__module__ == error_module.__name__
    ]


class TestErrors:
    def test_every_error_has_a_stable_code(self) -> None:
        assert NotFoundError("task", "AWAY-0001").code == "not_found"
        assert DependencyCycleError(["a", "b", "a"]).code == "dependency_cycle"

    def test_errors_serialise_for_machine_consumption(self) -> None:
        error = EvidenceIncompleteError(
            task_id="AWAY-0001", attempt_id="abc", missing=["tests"], failed=[]
        )
        payload = error.to_dict()
        assert payload["code"] == "evidence_incomplete"
        assert payload["details"]["missing"] == ["tests"]
        assert payload["details"]["task_id"] == "AWAY-0001"

    def test_details_are_ordered_for_stable_output(self) -> None:
        error = ClaudeAwayError("boom", zebra=1, alpha=2)
        assert list(error.details) == ["alpha", "zebra"]

    def test_cycle_error_reports_the_path(self) -> None:
        error = DependencyCycleError(["AWAY-0001", "AWAY-0002", "AWAY-0001"])
        assert "AWAY-0001 -> AWAY-0002 -> AWAY-0001" in error.message

    def test_all_domain_errors_derive_from_the_base(self) -> None:
        for klass in _error_classes():
            assert issubclass(klass, ClaudeAwayError), klass

    def test_every_error_class_is_exported(self) -> None:
        """`__all__` must list every error, including the intermediate base classes.

        Discovered by introspecting the module rather than by reading ``__all__``, because
        a test that iterates ``__all__`` cannot notice something missing from it -- which
        is exactly how ``StaleReplayError``, ``DagError`` and ``LeaseError`` went
        unexported. A supervisor catching a category (``except DagError``) needs the base
        classes as much as the leaves.
        """
        from claude_away import errors as error_module

        unexported = sorted(
            klass.__name__
            for klass in _error_classes()
            if klass.__name__ not in error_module.__all__
        )
        assert not unexported, f"error classes missing from __all__: {unexported}"

    def test_error_codes_are_unique(self) -> None:
        """A supervisor branches on these; two errors sharing a code would be ambiguous."""
        owners: dict[str, type[ClaudeAwayError]] = {}
        for klass in _error_classes():
            if klass is ClaudeAwayError:
                continue
            code = klass.code
            if code in owners:
                # Sharing a code is fine only by inheritance -- a subclass that does not
                # redefine it. Two unrelated classes with one code would be indistinguishable.
                sibling = owners[code]
                assert issubclass(klass, sibling) or issubclass(sibling, klass), (
                    f"{klass.__name__} and {sibling.__name__} both claim code {code!r}"
                )
            else:
                owners[code] = klass
