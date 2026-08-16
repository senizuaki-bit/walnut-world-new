"""Cross-field contract invariants that JSON Schema cannot express.

Adapters must call these functions after structural schema validation and before
committing, rendering, or returning data. They raise a typed exception instead
of returning a boolean, so callers cannot accidentally ignore a failed check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, NoReturn, cast


class ContractInvariantViolation(ValueError):
    """An explicit non-silent boundary failure with a catalog-compatible code."""

    code: str
    details: Mapping[str, object]

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


def _fail(code: str, message: str, **details: object) -> NoReturn:
    raise ContractInvariantViolation(code, message, details)


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("INVALID_REQUEST", f"{field_name} must be an integer >= {minimum}")
    return value


def _items(value: object, field_name: str, *, allow_empty: bool) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("INVALID_REQUEST", f"{field_name} must be an array")
    items = cast(Sequence[object], value)
    if not allow_empty and not items:
        _fail("INVALID_REQUEST", f"{field_name} cannot be empty")
    return items


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_REQUEST", f"{field_name} must be an object")
    return cast(Mapping[str, Any], value)


def _contiguous_events(
    events: Sequence[object],
    *,
    expected_first: int,
    label: str,
) -> int:
    expected = expected_first
    event_ids: set[str] = set()
    for index, raw_event in enumerate(events):
        event = _mapping(raw_event, f"{label}.events[{index}]")
        sequence = _integer(event.get("sequence"), f"{label}.events[{index}].sequence", minimum=1)
        if sequence != expected:
            _fail(
                "EVENT_SEQUENCE_GAP",
                f"{label} is not gap-free",
                expected_sequence=expected,
                actual_sequence=sequence,
            )
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            _fail("INVALID_REQUEST", f"{label}.events[{index}].event_id must be text")
        if event_id in event_ids:
            _fail("INVALID_REQUEST", f"{label} contains duplicate event_id", event_id=event_id)
        event_ids.add(event_id)
        expected += 1
    return expected - 1


def validate_client_event_batch(batch: Mapping[str, Any]) -> None:
    """Enforce sequence boundaries, ordering, and identity uniqueness atomically."""

    first = _integer(batch.get("first_sequence"), "batch.first_sequence", minimum=1)
    last = _integer(batch.get("last_sequence"), "batch.last_sequence", minimum=1)
    events = _items(batch.get("events"), "batch.events", allow_empty=False)
    actual_last = _contiguous_events(events, expected_first=first, label="client event batch")
    first_event = _mapping(events[0], "batch.events[0]")
    if first_event.get("sequence") != first or actual_last != last:
        _fail(
            "EVENT_SEQUENCE_GAP",
            "client event batch boundary fields disagree with its events",
            declared_first=first,
            declared_last=last,
            actual_first=first_event.get("sequence"),
            actual_last=actual_last,
        )


def validate_world_event_page(
    page: Mapping[str, Any],
    *,
    expected_after_sequence: int | None = None,
) -> None:
    """Reject an entire event page unless it is safe to advance the cursor."""

    world_id = page.get("world_id")
    if not isinstance(world_id, str) or not world_id:
        _fail("INVALID_REQUEST", "page.world_id must be text")
    from_sequence = _integer(page.get("from_sequence"), "page.from_sequence")
    to_sequence = _integer(page.get("to_sequence"), "page.to_sequence")
    next_after = _integer(page.get("next_after_sequence"), "page.next_after_sequence")
    if expected_after_sequence is not None:
        expected_after_sequence = _integer(expected_after_sequence, "expected_after_sequence")
    events = _items(page.get("events"), "page.events", allow_empty=True)

    if not events:
        expected_cursor = (
            expected_after_sequence if expected_after_sequence is not None else from_sequence
        )
        if (
            from_sequence != expected_cursor
            or to_sequence != expected_cursor
            or next_after != expected_cursor
        ):
            _fail("EVENT_SEQUENCE_GAP", "empty world event page advanced or changed its cursor")
        return

    first_event = _mapping(events[0], "page.events[0]")
    actual_first = _integer(first_event.get("sequence"), "page.events[0].sequence", minimum=1)
    expected_first = (
        expected_after_sequence + 1 if expected_after_sequence is not None else actual_first
    )
    actual_last = _contiguous_events(
        events, expected_first=expected_first, label="world event page"
    )
    for index, raw_event in enumerate(events):
        event = _mapping(raw_event, f"page.events[{index}]")
        if event.get("stream_id") != f"world:{world_id}":
            _fail(
                "EVENT_SEQUENCE_GAP",
                "world event stream does not match page.world_id",
                event_index=index,
                stream_id=event.get("stream_id"),
            )
    if from_sequence != actual_first or to_sequence != actual_last or next_after != actual_last:
        _fail(
            "EVENT_SEQUENCE_GAP",
            "world event page cursors disagree with its events",
            actual_first=actual_first,
            actual_last=actual_last,
        )


def validate_class_insights_privacy(result: Mapping[str, Any]) -> None:
    """Enforce the effective cohort threshold, including dynamic server policy."""

    privacy = _mapping(result.get("privacy"), "result.privacy")
    requested = _integer(
        privacy.get("minimum_cohort_size"),
        "result.privacy.minimum_cohort_size",
        minimum=5,
    )
    effective = _integer(
        privacy.get("effective_minimum_cohort_size"),
        "result.privacy.effective_minimum_cohort_size",
        minimum=5,
    )
    if effective < requested:
        _fail(
            "INVARIANT_VIOLATION",
            "effective privacy threshold cannot be lower than the requested threshold",
            requested=requested,
            effective=effective,
        )
    cohort_size = _integer(result.get("cohort_size"), "result.cohort_size")
    insights = _items(result.get("insights"), "result.insights", allow_empty=True)
    for index, raw_insight in enumerate(insights):
        insight = _mapping(raw_insight, f"result.insights[{index}]")
        suppressed = insight.get("suppressed")
        if not isinstance(suppressed, bool):
            _fail("INVALID_REQUEST", f"result.insights[{index}].suppressed must be boolean")
        learner_count = insight.get("learner_count")
        ratio = insight.get("ratio")
        if cohort_size < effective and not suppressed:
            _fail(
                "INVARIANT_VIOLATION",
                "all insights must be suppressed for a small cohort",
                index=index,
            )
        if suppressed:
            if learner_count is not None or ratio is not None:
                _fail(
                    "INVARIANT_VIOLATION", "suppressed insight leaked a count or ratio", index=index
                )
            continue
        count = _integer(learner_count, f"result.insights[{index}].learner_count")
        if count < effective:
            _fail(
                "INVARIANT_VIOLATION",
                "unsuppressed insight is below the effective cohort threshold",
                index=index,
                learner_count=count,
                effective=effective,
            )
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            _fail("INVALID_REQUEST", f"result.insights[{index}].ratio must be between 0 and 1")


__all__ = [
    "ContractInvariantViolation",
    "validate_class_insights_privacy",
    "validate_client_event_batch",
    "validate_world_event_page",
]
