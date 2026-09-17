"""Small exhaustive oracle independent of the MILP and its rule validator."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import product
from random import Random

import pytest

from ephios_shift_coordination.optimizer import propose_plan, validate_proposal
from ephios_shift_coordination.scheduling import (
    Availability,
    Commitment,
    Observer,
    Period,
    Person,
    PlanningInput,
    Rules,
    Shift,
)


def problem(*, days=(5, 7), people=3, minimum=2, maximum=3):
    shifts = tuple(
        Shift(
            i,
            i,
            1,
            datetime(2026, 10, day, 10, tzinfo=UTC),
            datetime(2026, 10, day, 12, tzinfo=UTC),
            date(2026, 10, day),
            minimum,
            maximum,
        )
        for i, day in enumerate(days, 1)
    )
    return PlanningInput(
        Period(1, date(2026, 10, 5), date(2026, 10, 31), "UTC", 1),
        Rules(2, True, 10),
        shifts,
        tuple(Person(i, 2, True) for i in range(1, people + 1)),
        tuple(
            Availability(i, s.id, "available", True) for i in range(1, people + 1) for s in shifts
        ),
        (),
    )


def oracle_score(data, pairs):
    """Return three ordered scores, or None when this complete alternative is invalid."""
    shifts = {s.id: s for s in data.shifts}
    ratings = {(a.person_id, a.shift_id): a for a in data.availability}
    counts = Counter(sid for uid, sid in pairs)
    if any(counts[s.id] not in (0, s.minimum) for s in data.shifts):
        return None
    for person in data.people:
        selected = [shifts[sid] for uid, sid in pairs if uid == person.id]
        baseline = [
            c
            for c in data.commitments
            if c.person_id == person.id and c.event_type_id == data.period.event_type_id
        ]
        if selected and (not person.complete or person.maximum is None):
            return None
        in_period = sum(
            data.period.start_date <= c.local_date <= data.period.end_date for c in baseline
        )
        if selected and len(selected) > max(0, person.maximum - in_period):
            return None
        for target in selected:
            answer = ratings.get((person.id, target.id))
            if (
                not answer
                or not answer.eligible
                or answer.rating not in ("available", "preferred", "if_needed")
            ):
                return None
            week = target.local_date.isocalendar()[:2]
            used = sum(c.local_date.isocalendar()[:2] == week for c in baseline)
            if sum(s.local_date.isocalendar()[:2] == week for s in selected) > max(
                0, data.rules.weekly_limit - used
            ):
                return None
            for other in baseline + [s for s in selected if s.id != target.id]:
                if target.start < other.end and other.start < target.end:
                    return None
                if (
                    data.rules.free_next_day
                    and abs((target.local_date - other.local_date).days) == 1
                ):
                    return None
    return (
        sum(bool(counts[s.id]) for s in data.shifts),
        -sum(ratings[pair].rating == "if_needed" for pair in pairs),
        sum(ratings[pair].rating == "preferred" for pair in pairs),
    )


def oracle(data):
    pairs = [(p.id, s.id) for p in data.people for s in data.shifts]
    scores = [
        oracle_score(data, tuple(pair for pair, bit in zip(pairs, bits, strict=True) if bit))
        for bits in product((False, True), repeat=len(pairs))
    ]
    return max(score for score in scores if score is not None)


def test_complete_shifts_take_priority_over_partial_or_extra_staffing():
    data = problem(people=2)
    data = replace(data, people=tuple(replace(p, maximum=1) for p in data.people))
    result = propose_plan(data)
    assert result.status == "optimal_primary"
    assert result.score.primary == oracle(data) == (1, 0, 0)
    assert len(result.assignments) == 2
    assert len(result.unfilled) == 1 and result.unfilled[0][1] == 2
    validate_proposal(data, result.assignments)


def test_filling_beats_yellow_then_yellow_beats_strong_preferences():
    data = problem(days=(5,), people=3)
    data = replace(
        data,
        availability=(
            Availability(1, 1, "if_needed", True),
            Availability(2, 1, "preferred", True),
            Availability(3, 1, "available", True),
        ),
    )
    result = propose_plan(data)
    assert result.assignments == ((2, 1), (3, 1))
    assert result.score.primary == (1, 0, 1)
    data = replace(data, availability=data.availability[:2])
    assert propose_plan(data).score.primary == (1, -1, 1)


@pytest.mark.parametrize("seed", range(12))
def test_real_solver_matches_exhaustive_oracle(seed):
    rng = Random(seed)
    data = problem(days=(5, 6, 8), minimum=1, people=3)
    data = replace(
        data,
        people=tuple(
            replace(p, maximum=rng.randrange(3), complete=rng.random() > 0.1) for p in data.people
        ),
        availability=tuple(
            replace(
                a,
                rating=rng.choice(("unavailable", "available", "if_needed", "preferred", None)),
                eligible=rng.random() > 0.15,
            )
            for a in data.availability
        ),
        commitments=(
            Commitment(
                10,
                1,
                rng.choice((1, 2)),
                datetime(2026, 10, 4, 10, tzinfo=UTC),
                datetime(2026, 10, 5, 11, tzinfo=UTC),
                date(2026, 10, 4),
            ),
        ),
    )
    result = propose_plan(data)
    assert result.status == "optimal_primary"
    assert result.score.primary == oracle(data)
    assert oracle_score(data, result.assignments) == result.score.primary
    assert result.assignments == tuple(sorted(set(result.assignments)))


def test_zero_capacity_and_missing_answers_are_valid_empty_proposals():
    for people in ((Person(1, 0, True),), (Person(1, None, False),)):
        data = replace(problem(people=1, minimum=1), people=people)
        result = propose_plan(data)
        assert result.status == "optimal_primary"
        assert result.assignments == () and result.score.primary == (0, 0, 0)


def test_baseline_over_limit_and_type_boundary_do_not_make_model_infeasible():
    data = problem(days=(5,), people=1, minimum=1)
    baseline = tuple(
        Commitment(
            i,
            1,
            1,
            datetime(2026, 10, 4, 10, tzinfo=UTC),
            datetime(2026, 10, 5, 11, tzinfo=UTC),
            date(2026, 10, 4),
        )
        for i in (10, 11, 12)
    )
    result = propose_plan(replace(data, commitments=baseline))
    assert result.status == "optimal_primary" and result.assignments == ()
    other = replace(data, commitments=tuple(replace(c, event_type_id=2) for c in baseline))
    assert propose_plan(other).assignments == ((1, 1),)


@pytest.mark.parametrize(
    "pairs",
    [((999, 1),), ((1, 999),), ((1, 1), (1, 1)), ((True, 1),), ((1, 1),), ((1, 1), (2, 1), (3, 1))],
)
def test_independent_proposal_validator_rejects_unknown_duplicate_partial_or_extra_assignments(
    pairs,
):
    with pytest.raises(ValueError):
        validate_proposal(problem(), pairs)


def test_partner_improvement_preserves_all_three_primary_scores():
    from ephios_shift_coordination.optimizer import improve_partners, score_plan

    data = problem(days=(5, 7), people=4)
    repeated = ((1, 1), (2, 1), (1, 2), (2, 2))
    from time import monotonic

    improved, examined = improve_partners(data, repeated, monotonic() + 1)
    assert 0 < examined <= 10_000
    assert score_plan(data, improved).primary == score_plan(data, repeated).primary
    assert score_plan(data, improved).partner_repeats < score_plan(data, repeated).partner_repeats
    assert oracle_score(data, improved) == oracle_score(data, repeated)


def test_partner_swaps_improve_when_every_person_is_at_capacity():
    from time import monotonic

    from ephios_shift_coordination.optimizer import improve_partners, score_plan

    data = problem(days=(5, 7, 9, 11), people=4)
    data = replace(data, rules=replace(data.rules, weekly_limit=4))
    repeated = ((1, 1), (2, 1), (1, 2), (2, 2), (3, 3), (4, 3), (3, 4), (4, 4))
    improved, examined = improve_partners(data, repeated, monotonic() + 1)
    assert examined > 0
    assert score_plan(data, improved).primary == score_plan(data, repeated).primary
    assert score_plan(data, improved).partner_repeats == 0
    assert oracle_score(data, improved) == oracle_score(data, repeated)
    assert Counter(uid for uid, sid in improved) == Counter(uid for uid, sid in repeated)


def test_expired_partner_budget_keeps_valid_incumbent():
    from ephios_shift_coordination.optimizer import improve_partners

    repeated = ((1, 1), (2, 1), (1, 2), (2, 2))
    assert improve_partners(problem(), repeated, 0) == (tuple(sorted(repeated)), 0)


def sitting_input(observers, **rules):
    """Two non-overlapping shifts on one day: one needs somebody sitting in."""
    start = datetime(2026, 10, 5, 9, tzinfo=UTC)
    shifts = (
        Shift(1, 1, 1, start, start + timedelta(hours=2), date(2026, 10, 5), 2, 3),
        Shift(
            2,
            2,
            1,
            start + timedelta(hours=3),
            start + timedelta(hours=5),
            date(2026, 10, 5),
            2,
            3,
        ),
    )
    qualified = {(1, 1), (2, 1), (3, 2)}
    return PlanningInput(
        Period(1, date(2026, 10, 5), date(2026, 10, 31), "UTC", 1),
        Rules(2, True, 10, allow_observers=True, **rules),
        shifts,
        tuple(Person(i, 2, True) for i in range(1, 5)),
        tuple(
            Availability(
                person,
                shift,
                "available" if (person, shift) in qualified else None,
                (person, shift) in qualified,
            )
            for person in range(1, 5)
            for shift in (1, 2)
        ),
        (),
        observers=observers,
    )


def test_people_sitting_in_fill_a_shift_but_never_alone():
    data = replace(problem(days=(5,), people=2, minimum=2, maximum=3), observers=(Observer(2, 1),))
    data = replace(
        data,
        rules=replace(data.rules, allow_observers=True),
        availability=(Availability(1, 1, "available", True), Availability(2, 1, None, False)),
    )
    result = propose_plan(data)
    assert result.status == "optimal_primary"
    assert result.assignments == ((1, 1),) and result.observers == ((2, 1),)
    assert result.score.filled == 1 and result.score.sitting == 1
    strict = propose_plan(replace(data, rules=replace(data.rules, minimum_regular=2)))
    assert strict.assignments == () and strict.observers == ()
    assert strict.unfilled == ((1, 2),)


def test_sitting_in_stays_the_exception_and_fills_only_what_regular_staff_cannot():
    data = sitting_input((Observer(1, 2), Observer(4, 2)))
    result = propose_plan(data)
    # Shift 2 needs one person sitting in; shift 1 is staffed regularly, so one is enough.
    assert result.score.filled == 2 and result.score.sitting == 1
    assert len(result.observers) == 1 and result.observers[0][1] == 2
    assert (3, 2) in result.assignments


def test_somebody_on_duty_that_day_may_sit_in_without_offering_it():
    """Person 2 serves the second shift, so staying for the first one costs them nothing."""
    start = datetime(2026, 10, 5, 9, tzinfo=UTC)
    shifts = (
        Shift(1, 1, 1, start, start + timedelta(hours=4), date(2026, 10, 5), 2, 3),
        Shift(
            2, 2, 1, start + timedelta(hours=4), start + timedelta(hours=8), date(2026, 10, 5), 2, 3
        ),
    )
    qualified = {(1, 1), (3, 1), (2, 2), (3, 2)}
    data = PlanningInput(
        Period(1, date(2026, 10, 5), date(2026, 10, 5), "UTC", 1),
        Rules(2, False, 10, allow_observers=True),
        shifts,
        tuple(Person(i, 1, True) for i in range(1, 4)),
        tuple(
            Availability(
                person,
                shift,
                "available" if (person, shift) in qualified else None,
                (person, shift) in qualified,
            )
            for person in range(1, 4)
            for shift in (1, 2)
        ),
        (),
    )
    result = propose_plan(data)
    assert result.score.filled == 2 and result.score.sitting == 1
    sitting_person, sitting_shift = result.observers[0]
    # Nobody offered to sit in, so the person sitting in must serve the other shift that day.
    assert (sitting_person, 3 - sitting_shift) in result.assignments
    # The second role is free: everybody keeps their personal maximum of one service.
    assert all(sum(uid == person for uid, _sid in result.assignments) <= 1 for person in (1, 2, 3))
    assert not propose_plan(
        replace(data, rules=replace(data.rules, allow_observers=False))
    ).observers


def test_the_independent_validator_rejects_sitting_in_without_an_offer():
    data = sitting_input(())
    with pytest.raises(ValueError):
        validate_proposal(data, ((1, 1), (2, 1), (3, 2)), ((4, 2),))
