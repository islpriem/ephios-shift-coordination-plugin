"""Deterministic synthetic load, also runnable in a bounded child process."""

import json
import os
import platform
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from random import Random
from time import perf_counter

import scipy

from ephios_shift_coordination.optimizer import propose_plan, validate_proposal
from ephios_shift_coordination.scheduling import (
    Availability,
    Commitment,
    Period,
    Person,
    PlanningInput,
    Rules,
    Shift,
)


def load_input(scarce=False):
    rng = Random(20260915)
    start_date = date(2033, 1, 1)
    shifts = []
    for index in range(200):
        day = start_date + timedelta(days=(index // 3) * 89 // 66)
        start = datetime(day.year, day.month, day.day, 8 + 4 * (index % 3), tzinfo=UTC)
        shifts.append(Shift(index + 1, index + 1, 1, start, start + timedelta(hours=4), day, 2, 3))
    people = tuple(
        Person(i, rng.randint(1, 3) if scarce else rng.randint(5, 9), i % 23 != 0)
        for i in range(1, 101)
    )
    availability = []
    for p in people:
        for shift in shifts:
            eligible = p.id % 3 == shift.id % 3 or p.id % 5 == 0
            if scarce:
                eligible = eligible and p.id % 4 == 0
            rating = rng.choices(
                ("unavailable", "if_needed", "available", "preferred"),
                weights=(20, 65, 10, 5) if scarce else (20, 10, 55, 15),
            )[0]
            availability.append(Availability(p.id, shift.id, rating, eligible))
    previous_start = datetime(2032, 12, 31, 10, tzinfo=UTC)
    commitments = tuple(
        Commitment(
            i, i, 1, previous_start, previous_start + timedelta(hours=4), previous_start.date()
        )
        for i in range(1, 21)
    )
    return PlanningInput(
        Period(1, start_date, date(2033, 3, 31), "UTC", 1),
        Rules(2, True, 10),
        tuple(shifts),
        people,
        tuple(availability),
        commitments,
    )


def benchmark(scarce=False):
    data = load_input(scarce)
    start = perf_counter()
    result = propose_plan(data)
    elapsed = perf_counter() - start
    if result.score is not None:
        validate_proposal(data, result.assignments)
    return {
        "case": "scarce" if scarce else "realistic",
        "seed": 20260915,
        "people": len(data.people),
        "shifts": len(data.shifts),
        "period": [str(data.period.start_date), str(data.period.end_date)],
        "budget_seconds": 10,
        "elapsed_seconds": elapsed,
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "scipy": scipy.__version__,
        },
        "result": asdict(result),
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(benchmark("--scarce" in sys.argv)))
