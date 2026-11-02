"""Concurrent telephone/chat duties with explicit roles and event boundaries."""

from dataclasses import replace
from datetime import timedelta
from itertools import combinations, product
from random import Random

import pytest

from ephios_shift_coordination.optimizer import propose_plan, validate_proposal
from ephios_shift_coordination.scheduling import Commitment, Observer, validate
from tests.test_optimizer import problem


def concurrent_problem():
    data = problem(days=(5, 5), people=2, minimum=2)
    return replace(
        data,
        rules=replace(data.rules, allow_observers=True),
        shifts=tuple(replace(shift, event_id=10) for shift in data.shifts),
        people=tuple(replace(person, maximum=1) for person in data.people),
        availability=tuple(
            replace(answer, rating="preferred" if answer.shift_id == 1 else "available")
            for answer in data.availability
        ),
    )


def test_parallel_services_fill_before_strong_telephone_preferences():
    data = concurrent_problem()
    result = propose_plan(data)
    assert result.status == "optimal_primary"
    assert result.score.filled == 2
    assert result.score.sitting == 2
    assert len(result.assignments) == 2
    assert len({uid for uid, _sid in result.assignments}) == 2
    validate_proposal(data, result.assignments, result.observers)
    checked = validate(data, result.assignments, result.observers)
    assert all(count["total"] == 1 for count in checked.counts.values())


@pytest.mark.parametrize("other_event", [None, 11])
def test_parallel_observer_exception_needs_the_same_known_event(other_event):
    data = concurrent_problem()
    data = replace(data, shifts=(data.shifts[0], replace(data.shifts[1], event_id=other_event)))
    assert propose_plan(data).score.filled == 1
    checked = validate(data, ((1, 1), (2, 2)), ((1, 2), (2, 1)))
    assert "overlap" in {v.code for v in checked.violations}


def test_two_parallel_regular_assignments_stay_forbidden():
    data = concurrent_problem()
    checked = validate(data, ((1, 1), (1, 2)))
    assert "overlap" in {v.code for v in checked.violations}


def test_regular_minimum_and_disabled_observers_are_not_bypassed():
    data = concurrent_problem()
    for rules in (
        replace(data.rules, minimum_regular=2),
        replace(data.rules, allow_observers=False),
    ):
        assert propose_plan(replace(data, rules=rules)).score.filled == 1


@pytest.mark.parametrize("days_later", [0, 6])
def test_same_event_observers_can_span_midnight_and_week_boundaries(days_later):
    data = concurrent_problem()
    first = replace(
        data.shifts[0],
        start=data.shifts[0].start + timedelta(days=days_later, hours=13),
        end=data.shifts[0].end + timedelta(days=days_later, hours=15),
        local_date=data.shifts[0].local_date + timedelta(days=days_later),
    )
    second = replace(
        data.shifts[1],
        start=first.start + timedelta(hours=2),
        end=first.end + timedelta(hours=1),
        local_date=first.local_date + timedelta(days=1),
    )
    data = replace(data, shifts=(first, second))
    result = propose_plan(data)
    assert result.score.filled == 2
    assert result.score.ranking() == role_oracle(data)
    assert all(
        c["total"] == 1
        for c in validate(data, result.assignments, result.observers).counts.values()
    )


def role_oracle(data):
    """Enumerate absent/regular/observer roles without calling the production validator."""
    shifts = {s.id: s for s in data.shifts}
    answers = {(a.person_id, a.shift_id): a for a in data.availability}
    offers = {(o.person_id, o.shift_id) for o in data.observers}
    pairs = [(p.id, s.id) for p in data.people for s in data.shifts]

    def score(roles):
        regular = [pair for pair, role in zip(pairs, roles, strict=True) if role == 1]
        watching = [pair for pair, role in zip(pairs, roles, strict=True) if role == 2]
        filled = 0
        for shift in data.shifts:
            main = sum(sid == shift.id for _uid, sid in regular)
            extra = sum(sid == shift.id for _uid, sid in watching)
            if main + extra not in (0, shift.minimum):
                return None
            if extra and (
                not data.rules.allow_observers
                or main < min(data.rules.minimum_regular, shift.minimum)
            ):
                return None
            filled += main + extra > 0
        for person in data.people:
            active = [(shifts[sid], False) for uid, sid in regular if uid == person.id]
            active += [(shifts[sid], True) for uid, sid in watching if uid == person.id]
            if active and not person.complete:
                return None
            native = [
                (c, c.observer)
                for c in data.commitments
                if c.person_id == person.id and c.event_type_id == data.period.event_type_id
            ]
            for shift, observer in active:
                answer = answers.get((person.id, shift.id))
                own = any(
                    not obs and other.event_id == shift.event_id for other, obs in active + native
                )
                if observer:
                    if answer and answer.rating == "unavailable":
                        return None
                    if (person.id, shift.id) not in offers and not own:
                        return None
                elif (
                    not answer
                    or not answer.eligible
                    or answer.rating not in ("preferred", "available", "if_needed")
                ):
                    return None
            entries = [(shift, obs, True) for shift, obs in active] + [
                (c, obs, False) for c, obs in native
            ]
            for (a, a_obs, a_new), (b, b_obs, b_new) in combinations(entries, 2):
                if not (a_new or b_new):
                    continue
                if a.event_id is not None and a.event_id == b.event_id and (a_obs or b_obs):
                    continue
                if a.start < b.end and b.start < a.end:
                    return None
                if data.rules.free_next_day and abs((a.local_date - b.local_date).days) == 1:
                    return None
            serving = {s.event_id for s, observer in active + native if not observer}
            load = [
                (s, new)
                for s, observer, new in entries
                if not observer or s.event_id not in serving
            ]
            in_period = [
                (s, new)
                for s, new in load
                if data.period.start_date <= s.local_date <= data.period.end_date
            ]
            if any(new for _s, new in in_period) and len(in_period) > person.maximum:
                return None
            for week in {s.local_date.isocalendar()[:2] for s in data.shifts}:
                week_load = [(s, new) for s, new in load if s.local_date.isocalendar()[:2] == week]
                if any(new for _s, new in week_load) and len(week_load) > data.rules.weekly_limit:
                    return None
        return (
            filled,
            -len(watching),
            -sum(answers[p].rating == "if_needed" for p in regular),
            sum(answers[p].rating == "preferred" for p in regular),
        )

    feasible = [
        s for roles in product(range(3), repeat=len(pairs)) if (s := score(roles)) is not None
    ]
    return max(feasible)


@pytest.mark.parametrize("seed", range(36))
def test_observer_solver_matches_independent_exhaustive_role_oracle(seed):
    rng = Random(seed)
    data = problem(days=(5, 5), people=3, minimum=2)
    first = replace(data.shifts[0], event_id=10)
    offset = timedelta(days=seed % 3)
    second = replace(
        data.shifts[1],
        event_id=10 if seed % 3 == 0 else 11,
        start=first.start + offset,
        end=first.end + offset,
        local_date=first.local_date + offset,
    )
    data = replace(
        data,
        shifts=(first, second),
        rules=replace(
            data.rules,
            allow_observers=True,
            minimum_regular=1 + (seed % 7 == 0),
            free_next_day=bool(seed % 2),
        ),
        people=tuple(
            replace(p, maximum=rng.randrange(3), complete=rng.random() > 0.1) for p in data.people
        ),
        availability=tuple(
            replace(
                a,
                rating=rng.choice((None, "unavailable", "available", "if_needed", "preferred")),
                eligible=rng.random() > 0.2,
            )
            for a in data.availability
        ),
        observers=tuple(
            Observer(p.id, s.id) for p in data.people for s in data.shifts if rng.random() > 0.5
        ),
    )
    expected = role_oracle(data)
    result = propose_plan(data)
    assert result.status == "optimal_primary", result
    assert result.score.ranking() == expected
    validate_proposal(data, result.assignments, result.observers)


def test_existing_regular_duty_allows_an_unoffered_parallel_observer_at_personal_limit():
    data = concurrent_problem()
    shift = data.shifts[0]
    existing = Commitment(50, 1, 1, shift.start, shift.end, shift.local_date, event_id=10)
    data = replace(
        data,
        shifts=(data.shifts[1],),
        commitments=(existing,),
        availability=tuple(a for a in data.availability if a.shift_id == 2 and a.person_id == 2),
    )
    result = propose_plan(data)
    assert result.assignments == ((2, 2),) and result.observers == ((1, 2),)
    assert result.score.ranking() == role_oracle(data)


def test_existing_observer_of_another_event_blocks_overlap_and_uses_capacity():
    data = concurrent_problem()
    shift = data.shifts[0]
    existing = Commitment(
        50, 1, 1, shift.start, shift.end, shift.local_date, event_id=20, observer=True
    )
    for offset in (timedelta(), timedelta(hours=-4)):
        baseline = replace(existing, start=existing.start + offset, end=existing.end + offset)
        result = propose_plan(replace(data, commitments=(baseline,)))
        assert result.score.filled == 0
    foreign = replace(data, commitments=(replace(existing, event_type_id=2),))
    assert propose_plan(foreign).score.filled == 2


def test_red_blocks_both_roles_even_beside_an_own_duty():
    data = concurrent_problem()
    data = replace(
        data,
        availability=tuple(
            replace(a, rating="preferred" if a.person_id == a.shift_id else "unavailable")
            for a in data.availability
        ),
    )
    result = propose_plan(data)
    assert result.assignments == result.observers == ()
    assert result.score.filled == 0
    assert "unavailable" in {
        v.code for v in validate(data, ((1, 1), (2, 2)), ((1, 2), (2, 1))).violations
    }


def test_one_regular_duty_allows_multiple_simultaneous_sibling_observers():
    data = problem(days=(5, 5, 5), people=3, minimum=2)
    data = replace(
        data,
        rules=replace(data.rules, allow_observers=True, weekly_limit=1),
        shifts=tuple(replace(s, event_id=10) for s in data.shifts),
        people=tuple(replace(p, maximum=1) for p in data.people),
        availability=tuple(
            replace(a, eligible=a.person_id == a.shift_id) for a in data.availability
        ),
    )
    checked = validate_proposal(data, ((1, 1), (2, 2), (3, 3)), ((1, 2), (1, 3), (2, 1)))
    assert all(c["total"] == 1 for c in checked.counts.values())
    result = propose_plan(data)
    assert result.score.filled == 3
    assert result.score.ranking() == role_oracle(data)


@pytest.mark.parametrize(
    "maximum, offered, expected_filled", [(0, True, 0), (1, False, 0), (1, True, 0)]
)
def test_red_blocks_observers_despite_offers_and_capacity(maximum, offered, expected_filled):
    data = concurrent_problem()
    data = replace(
        data,
        shifts=data.shifts[:1],
        people=(replace(data.people[0], maximum=maximum), data.people[1]),
        availability=tuple(
            replace(a, rating="unavailable" if a.person_id == 1 else "preferred")
            for a in data.availability
            if a.shift_id == 1
        ),
        observers=(Observer(1, 1),) if offered else (),
    )
    result = propose_plan(data)
    assert result.score.filled == expected_filled
    assert result.score.ranking() == role_oracle(data)


@pytest.mark.parametrize("invalid", [False, True])
def test_timed_out_parallel_role_result_is_independently_validated(monkeypatch, invalid):
    from types import SimpleNamespace

    from ephios_shift_coordination import optimizer

    # Four regular choices, two staffed flags, four observer choices; no offered-role credits.
    vector = [1, 0, 0, 1, 1, 1, 0, 1, 1, 0]
    if invalid:
        # Replacing the observers with regular duties breaches overlap and personal limits.
        vector = [1, 1, 1, 1, 1, 1, 0, 0, 0, 0]
    monkeypatch.setattr(
        optimizer, "milp", lambda *args, **kwargs: SimpleNamespace(status=1, x=vector)
    )
    result = propose_plan(concurrent_problem())
    assert result.status == ("no_proposal" if invalid else "feasible_timeout")
    assert result.diagnostics["proven_stages"] == []
    if invalid:
        assert result.assignments == result.observers == ()
    else:
        assert result.assignments == ((1, 1), (2, 2))
        assert result.observers == ((1, 2), (2, 1))
