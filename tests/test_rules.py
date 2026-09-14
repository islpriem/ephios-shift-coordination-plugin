from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from ephios_shift_coordination.scheduling import (
    Availability,
    Commitment,
    Period,
    Person,
    PlanningInput,
    Rules,
    Shift,
    validate,
)


def shift(pk=1, day=5, hour=10, **kwargs):
    start = datetime(2026, 10, day, hour, tzinfo=UTC)
    return Shift(pk, pk, 1, start, start + timedelta(hours=2), start.date(), 1, **kwargs)


def planning_input(**kwargs):
    return PlanningInput(
        period=Period(1, date(2026, 10, 5), date(2026, 10, 31), "Europe/Berlin", 1),
        rules=Rules(2, True, 10),
        **{
            "shifts": (shift(maximum=2),),
            "people": (Person(1, 2, True),),
            "availability": (Availability(1, 1, "preferred", True),),
            "commitments": (),
            **kwargs,
        },
    )


def codes(result):
    return {violation.code for violation in result.violations}


def test_empty_and_valid_drafts_count_shifts_without_mutating_input():
    data = planning_input()
    assert validate(data, ()).underfilled == ((1, 0, 1),)
    result = validate(data, ((1, 1),))
    assert not result.violations and not result.underfilled
    assert result.counts[1] == {"existing": 0, "draft": 1, "total": 1, "maximum": 2}
    assert data.people[0].maximum == 2


@pytest.mark.parametrize(
    ("person", "availability", "expected"),
    [
        (
            Person(1, 0, True),
            Availability(1, 1, "unavailable", False),
            {"personal_maximum", "unavailable", "qualification"},
        ),
        (Person(1, None, False), Availability(1, 1, None, True), {"missing_response"}),
        (Person(1, 2, True), Availability(1, 1, None, True), {"missing_response"}),
    ],
)
def test_manual_exceptions_do_not_change_answers(person, availability, expected):
    data = planning_input(people=(person,), availability=(availability,))
    assert codes(validate(data, ((1, 1),))) == expected
    assert data.people == (person,) and data.availability == (availability,)


def test_same_type_overlap_individual_dates_and_boundary_consecutive_days():
    target = shift()
    previous = Commitment(10, 1, 1, target.start - timedelta(days=1), target.end, date(2026, 10, 4))
    data = planning_input(commitments=(previous,))
    result = validate(data, ((1, 1),))
    assert codes(result) == {"overlap", "consecutive_days"}
    assert result.counts[1]["existing"] == 0
    assert not validate(
        replace(data, commitments=(replace(previous, event_type_id=2),)), ((1, 1),)
    ).violations


def test_adjacent_same_day_shifts_count_towards_week_and_person_limits():
    shifts = (shift(), shift(2, hour=12), shift(3, hour=14))
    data = planning_input(
        shifts=shifts, availability=tuple(Availability(1, s.id, "available", True) for s in shifts)
    )
    result = validate(data, ((1, 1), (1, 2), (1, 3)))
    assert codes(result) == {"personal_maximum", "weekly_limit"}
    assert result.counts[1]["total"] == 3


def test_existing_violations_are_reported_and_never_grant_extra_capacity():
    target = shift()
    commitments = tuple(
        Commitment(i, 1, 1, target.start, target.end, target.local_date) for i in (10, 11, 12)
    )
    data = planning_input(commitments=commitments)
    empty = validate(data, ())
    assert not empty.violations
    assert {v.code for v in empty.existing_violations} == {
        "overlap",
        "personal_maximum",
        "weekly_limit",
    }
    assert {"weekly_limit", "personal_maximum"} <= codes(validate(data, ((1, 1),)))


def test_shift_maximum_and_stable_fact_bound_tokens():
    data = planning_input(
        shifts=(shift(maximum=1),),
        people=(Person(1, 2, True), Person(2, 2, True)),
        availability=(Availability(1, 1, "available", True), Availability(2, 1, "available", True)),
    )
    first = validate(data, ((1, 1), (2, 1)))
    assert codes(first) == {"shift_maximum"}
    assert first.violations == validate(data, ((2, 1), (1, 1))).violations
    assert (
        first.violations[0].token
        != validate(replace(data, shifts=(shift(maximum=0),)), ((1, 1), (2, 1))).violations[0].token
    )


@pytest.mark.parametrize("pairs", [((99, 1),), ((1, 99),), ((1, 1), (1, 1)), ((True, 1),)])
def test_unknown_or_duplicate_assignments_are_integrity_errors(pairs):
    with pytest.raises(ValueError):
        validate(planning_input(), pairs)


def test_touched_week_counts_outside_interval_and_consecutive_rule_can_be_disabled():
    target = shift(day=7)
    previous = Commitment(
        10,
        1,
        1,
        target.start - timedelta(days=1),
        target.end - timedelta(days=1),
        date(2026, 10, 6),
    )
    data = planning_input(shifts=(target,), commitments=(previous,))
    data = replace(
        data, period=replace(data.period, start_date=date(2026, 10, 7)), rules=Rules(1, False, 10)
    )
    result = validate(data, ((1, 1),))
    assert codes(result) == {"weekly_limit"}
    assert result.counts[1]["existing"] == 0


def test_missing_availability_is_not_green_and_unlimited_shift_capacity_is_valid():
    data = planning_input(availability=())
    assert codes(validate(data, ((1, 1),))) == {"missing_response", "qualification"}
    assert not validate(planning_input(shifts=(shift(),)), ((1, 1),)).violations


def test_consecutive_means_adjacent_start_dates_not_adjacent_offered_dates():
    data = planning_input(
        shifts=(shift(day=4), shift(2, day=6)),
        availability=(Availability(1, 1, "if_needed", True), Availability(1, 2, "available", True)),
    )
    data = replace(data, period=replace(data.period, start_date=date(2026, 10, 1)))
    assert not validate(data, ((1, 1), (1, 2))).violations
