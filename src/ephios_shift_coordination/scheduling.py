"""Storage-independent scheduling data and authoritative business-rule checks."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from itertools import combinations


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Period:
    id: int
    start_date: date
    end_date: date
    timezone: str
    event_type_id: int


@dataclass(frozen=True)
class Rules:
    weekly_limit: int
    free_next_day: bool
    solver_seconds: int
    minimum_regular: int = 1
    allow_observers: bool = False


@dataclass(frozen=True)
class Shift:
    id: int
    native_id: int
    event_type_id: int
    start: datetime
    end: datetime
    local_date: date
    minimum: int
    maximum: int | None = None


@dataclass(frozen=True)
class Person:
    id: int
    maximum: int | None
    complete: bool


@dataclass(frozen=True)
class Availability:
    person_id: int
    shift_id: int
    rating: str | None
    eligible: bool


@dataclass(frozen=True)
class Observer:
    """A wish to sit in on a shift: no qualification needed, counted as staff."""

    person_id: int
    shift_id: int


@dataclass(frozen=True)
class Commitment:
    id: int
    person_id: int
    event_type_id: int
    start: datetime
    end: datetime
    local_date: date


@dataclass(frozen=True)
class PlanningInput:
    period: Period
    rules: Rules
    shifts: tuple[Shift, ...]
    people: tuple[Person, ...]
    availability: tuple[Availability, ...]
    commitments: tuple[Commitment, ...]
    schema_version: int = 1
    observers: tuple[Observer, ...] = ()


@dataclass(frozen=True)
class Violation:
    code: str
    person_ids: tuple[int, ...]
    shift_ids: tuple[int, ...]
    facts: dict

    @property
    def token(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class ValidationResult:
    violations: tuple[Violation, ...]
    existing_violations: tuple[Violation, ...]
    underfilled: tuple[tuple[int, int, int], ...]
    counts: dict[int, dict]


def week_start(day):
    return day - timedelta(days=day.weekday())


def validate(data: PlanningInput, assignments, observers=()) -> ValidationResult:
    """Validate a complete manual draft; native commitments are read-only context.

    Assignment pairs are (person_id, planned_shift_id). Observer pairs sit in without a
    qualification check, but count as staff. Sitting in on a day the person already serves
    needs no separate offer and is no extra service: the person is on site anyway.
    Integrity/access checking belongs to the adapter; unknown and duplicate pairs,
    including one person in both roles of a shift, are always rejected here.
    """
    shifts = {shift.id: shift for shift in data.shifts}
    people = {person.id: person for person in data.people}
    pairs = tuple(assignments)
    sitting = tuple(observers)
    everything = pairs + sitting
    if any(
        len(pair) != 2
        or any(type(value) is not int for value in pair)
        or pair[0] not in people
        or pair[1] not in shifts
        for pair in everything
    ) or len(set(everything)) != len(everything):
        raise ValueError("Unknown or duplicate assignments.")
    pairs, sitting = sorted(pairs), sorted(sitting)
    availability = {(a.person_id, a.shift_id): a for a in data.availability}
    wishes = {(o.person_id, o.shift_id) for o in data.observers}
    violations, existing = [], []
    counts, underfilled = {}, []
    duty = {person.id: set() for person in data.people}
    for person_id, shift_id in pairs:
        duty[person_id].add(shifts[shift_id].local_date)
    for commitment in data.commitments:
        if commitment.event_type_id == data.period.event_type_id:
            duty[commitment.person_id].add(commitment.local_date)

    def add(code, person_ids, selected, facts, baseline=False):
        target = existing if baseline else violations
        target.append(Violation(code, tuple(person_ids), tuple(sorted(selected)), facts))

    for person_id, shift_id in pairs:
        answer = availability.get((person_id, shift_id))
        facts = {
            "person": asdict(people[person_id]),
            "shift": asdict(shifts[shift_id]),
            "answer": asdict(answer) if answer else None,
        }
        if not people[person_id].complete or not answer or answer.rating is None:
            add("missing_response", [person_id], [shift_id], facts)
        if answer and answer.rating == "unavailable":
            add("unavailable", [person_id], [shift_id], facts)
        if not answer or not answer.eligible:
            add("qualification", [person_id], [shift_id], facts)

    for person_id, shift_id in sitting:
        answer = availability.get((person_id, shift_id))
        facts = {
            "person": asdict(people[person_id]),
            "shift": asdict(shifts[shift_id]),
            "answer": asdict(answer) if answer else None,
            "observer": True,
        }
        # Whoever already serves that day may stay for another shift without offering it.
        if not people[person_id].complete or (
            (person_id, shift_id) not in wishes
            and shifts[shift_id].local_date not in duty[person_id]
        ):
            add("observer_wish", [person_id], [shift_id], facts)
        if answer and answer.rating == "unavailable":
            add("unavailable", [person_id], [shift_id], facts)

    for shift in data.shifts:
        regular = [person_id for person_id, shift_id in pairs if shift_id == shift.id]
        extra = [person_id for person_id, shift_id in sitting if shift_id == shift.id]
        selected = sorted(regular + extra)
        if len(selected) < shift.minimum:
            underfilled.append((shift.id, len(selected), shift.minimum))
            # An empty shift simply does not happen; a partly staffed one sends people to a
            # service that cannot take place, so it needs a deliberate decision.
            if selected:
                add(
                    "shift_incomplete",
                    selected,
                    [shift.id],
                    {"count": len(selected), "minimum": shift.minimum, "shift": asdict(shift)},
                )
        if shift.maximum is not None and len(selected) > shift.maximum:
            add(
                "shift_maximum",
                selected,
                [shift.id],
                {"count": len(selected), "maximum": shift.maximum, "shift": asdict(shift)},
            )
        # Observers never staff a shift on their own.
        required = min(data.rules.minimum_regular, shift.minimum)
        if extra and len(regular) < required:
            add(
                "regular_minimum",
                selected,
                [shift.id],
                {"regular": regular, "observers": extra, "required": required},
            )

    touched_weeks = {week_start(shift.local_date) for shift in data.shifts}
    for person in data.people:
        # The boolean marks draft entries; native IDs and shift IDs are different namespaces.
        entries = [
            (False, c)
            for c in data.commitments
            if c.person_id == person.id and c.event_type_id == data.period.event_type_id
        ]
        entries += [(True, shifts[sid]) for uid, sid in pairs if uid == person.id]
        watching = [(True, shifts[sid]) for uid, sid in sitting if uid == person.id]
        # Sitting in beside an own service of that day costs no capacity, but still binds
        # the person to the day: overlaps and neighboring days are checked for both roles.
        served = {entry.local_date for _draft, entry in entries}
        load = entries + [(d, e) for d, e in watching if e.local_date not in served]
        entries += watching
        interval = [
            (draft, entry)
            for draft, entry in load
            if data.period.start_date <= entry.local_date <= data.period.end_date
        ]
        drafted = sum(draft for draft, _entry in interval)
        counts[person.id] = {
            "existing": len(interval) - drafted,
            "draft": drafted,
            "total": len(interval),
            "maximum": person.maximum,
        }

        def check_limit(code, subset, maximum, person_id=person.id, **facts):
            if maximum is not None and len(subset) > maximum:
                selected = [entry.id for draft, entry in subset if draft]
                add(
                    code,
                    [person_id],
                    selected,
                    {
                        "maximum": maximum,
                        "entries": [(draft, asdict(e)) for draft, e in subset],
                        **facts,
                    },
                    baseline=not selected,
                )

        check_limit("personal_maximum", interval, person.maximum)
        for week in sorted(touched_weeks):
            check_limit(
                "weekly_limit",
                [(draft, e) for draft, e in load if week_start(e.local_date) == week],
                data.rules.weekly_limit,
                week=week,
            )
        for (first_draft, first), (second_draft, second) in combinations(entries, 2):
            selected = [
                e.id for draft, e in ((first_draft, first), (second_draft, second)) if draft
            ]
            facts = {"entries": [(first_draft, asdict(first)), (second_draft, asdict(second))]}
            if first.start < second.end and second.start < first.end:
                add("overlap", [person.id], selected, facts, baseline=not selected)
            if data.rules.free_next_day and abs((first.local_date - second.local_date).days) == 1:
                add("consecutive_days", [person.id], selected, facts, baseline=not selected)
    return ValidationResult(tuple(violations), tuple(existing), tuple(underfilled), counts)
