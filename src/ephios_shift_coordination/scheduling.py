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


def validate(data: PlanningInput, assignments) -> ValidationResult:
    """Validate a complete manual draft; native commitments are read-only context.

    Assignment pairs are (person_id, planned_shift_id). Integrity/access checking
    belongs to the adapter; unknown and duplicate pairs are always rejected here.
    """
    shifts = {shift.id: shift for shift in data.shifts}
    people = {person.id: person for person in data.people}
    pairs = tuple(assignments)
    if any(
        len(pair) != 2
        or any(type(value) is not int for value in pair)
        or pair[0] not in people
        or pair[1] not in shifts
        for pair in pairs
    ) or len(set(pairs)) != len(pairs):
        raise ValueError("Unknown or duplicate assignments.")
    pairs = sorted(pairs)
    availability = {(a.person_id, a.shift_id): a for a in data.availability}
    violations, existing = [], []
    counts, underfilled = {}, []

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

    for shift in data.shifts:
        selected = [person_id for person_id, shift_id in pairs if shift_id == shift.id]
        if len(selected) < shift.minimum:
            underfilled.append((shift.id, len(selected), shift.minimum))
        if shift.maximum is not None and len(selected) > shift.maximum:
            add(
                "shift_maximum",
                selected,
                [shift.id],
                {"count": len(selected), "maximum": shift.maximum, "shift": asdict(shift)},
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
        interval = [
            (draft, entry)
            for draft, entry in entries
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
                [(draft, e) for draft, e in entries if week_start(e.local_date) == week],
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
