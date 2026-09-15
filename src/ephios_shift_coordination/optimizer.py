"""Pure, budgeted MILP proposals with independent feasibility validation."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import combinations
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csc_array

from .scheduling import PlanningInput, validate, week_start

STAGES = ("filled", "yellow", "preferred")


class Status(StrEnum):
    OPTIMAL_PRIMARY = "optimal_primary"
    FEASIBLE_TIMEOUT = "feasible_timeout"
    NO_PROPOSAL = "no_proposal"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True)
class Score:
    filled: int
    yellow: int
    preferred: int
    partner_repeats: int

    @property
    def primary(self):
        return self.filled, -self.yellow, self.preferred


@dataclass(frozen=True)
class PlanningResult:
    assignments: tuple[tuple[int, int], ...]
    score: Score | None
    unfilled: tuple[tuple[int, int], ...]
    status: Status
    diagnostics: dict
    schema_version: int = 1


def validate_input(data):
    """Reject inconsistent IDs, types and local dates at the pure module boundary."""

    def integer(value, minimum=0):
        return type(value) is int and value >= minimum

    if (
        not isinstance(data, PlanningInput)
        or type(data.schema_version) is not int
        or data.schema_version != 1
    ):
        raise ValueError("schema")
    period, rules = data.period, data.rules
    if (
        type(period.start_date) is not date
        or type(period.end_date) is not date
        or period.start_date > period.end_date
        or not integer(period.id, 1)
        or not integer(period.event_type_id, 1)
        or not integer(rules.weekly_limit)
        or not integer(rules.solver_seconds, 1)
        or type(rules.free_next_day) is not bool
    ):
        raise ValueError("period_or_rules")
    try:
        zone = ZoneInfo(period.timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("timezone") from exc
    people = {p.id: p for p in data.people}
    shifts = {s.id: s for s in data.shifts}
    if len(people) != len(data.people) or len(shifts) != len(data.shifts):
        raise ValueError("duplicate_ids")
    for p in data.people:
        if (
            not integer(p.id, 1)
            or type(p.complete) is not bool
            or (p.maximum is not None and not integer(p.maximum))
            or (p.complete and p.maximum is None)
        ):
            raise ValueError("person")
    for entry in (*data.shifts, *data.commitments):
        if (
            not integer(entry.id, 1)
            or not integer(entry.event_type_id, 1)
            or not isinstance(entry.start, datetime)
            or not isinstance(entry.end, datetime)
            or entry.start.utcoffset() != timedelta(0)
            or entry.end.utcoffset() != timedelta(0)
            or entry.start >= entry.end
            or type(entry.local_date) is not date
            or entry.local_date != entry.start.astimezone(zone).date()
        ):
            raise ValueError("times")
    for s in data.shifts:
        if (
            not integer(s.native_id, 1)
            or s.event_type_id != period.event_type_id
            or not period.start_date <= s.local_date <= period.end_date
            or not integer(s.minimum, 1)
            or (s.maximum is not None and (not integer(s.maximum) or s.maximum < s.minimum))
        ):
            raise ValueError("shift")
    if len({c.id for c in data.commitments}) != len(data.commitments) or any(
        not integer(c.person_id, 1) or c.person_id not in people for c in data.commitments
    ):
        raise ValueError("commitment")
    seen = set()
    for a in data.availability:
        key = a.person_id, a.shift_id
        if (
            key in seen
            or not integer(a.person_id, 1)
            or not integer(a.shift_id, 1)
            or a.person_id not in people
            or a.shift_id not in shifts
            or type(a.eligible) is not bool
            or a.rating not in (None, "unavailable", "if_needed", "available", "preferred")
        ):
            raise ValueError("availability")
        seen.add(key)


def validate_proposal(data, assignments):
    """Check business rules and exactly-minimum-or-zero, independently of MILP rows."""
    result = validate(data, assignments)
    counts = Counter(sid for _uid, sid in assignments)
    if result.violations or any(counts[s.id] not in (0, s.minimum) for s in data.shifts):
        raise ValueError("invalid_proposal")
    return result


def score_plan(data, assignments):
    ratings = {(a.person_id, a.shift_id): a.rating for a in data.availability}
    teams = defaultdict(list)
    for uid, sid in assignments:
        teams[sid].append(uid)
    partners = Counter(pair for team in teams.values() for pair in combinations(sorted(team), 2))
    return Score(
        len(teams),
        sum(ratings[pair] == "if_needed" for pair in assignments),
        sum(ratings[pair] == "preferred" for pair in assignments),
        sum(max(0, count - 1) for count in partners.values()),
    )


def conflict(first, second, free_next_day):
    return (first.start < second.end and second.start < first.end) or (
        free_next_day and abs((first.local_date - second.local_date).days) == 1
    )


class BudgetExpired(Exception):
    pass


def check_time(deadline):
    if monotonic() >= deadline:
        raise BudgetExpired


@dataclass
class Model:
    pairs: tuple
    shifts: tuple
    constraints: LinearConstraint
    objectives: tuple


def build_model(data, deadline):
    people = {p.id: p for p in data.people}
    shifts = {s.id: s for s in data.shifts}
    baseline = defaultdict(list)
    for commitment in data.commitments:
        if commitment.event_type_id == data.period.event_type_id:
            baseline[commitment.person_id].append(commitment)
    personal, weekly = {}, {}
    for p in data.people:
        check_time(deadline)
        personal[p.id] = max(
            0,
            (p.maximum or 0)
            - sum(
                data.period.start_date <= c.local_date <= data.period.end_date
                for c in baseline[p.id]
            ),
        )
        for week in {week_start(s.local_date) for s in data.shifts}:
            weekly[p.id, week] = max(
                0,
                data.rules.weekly_limit
                - sum(week_start(c.local_date) == week for c in baseline[p.id]),
            )
    candidates = []
    for answer in data.availability:
        check_time(deadline)
        p, s = people[answer.person_id], shifts[answer.shift_id]
        if (
            p.complete
            and answer.eligible
            and answer.rating in ("available", "if_needed", "preferred")
            and personal[p.id]
            and weekly[p.id, week_start(s.local_date)]
            and not any(conflict(s, c, data.rules.free_next_day) for c in baseline[p.id])
        ):
            candidates.append((p.id, s.id))
    pairs = tuple(sorted(candidates))
    targets = tuple(sorted(shifts))
    indices = {pair: i for i, pair in enumerate(pairs)}
    by_shift, by_person, by_week = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, (uid, sid) in enumerate(pairs):
        by_shift[sid].append(i)
        by_person[uid].append(i)
        by_week[uid, week_start(shifts[sid].local_date)].append(i)
    rows, cols, values, lower, upper = [], [], [], [], []

    def row(entries, lo, hi):
        number = len(lower)
        for col, value in entries:
            rows.append(number)
            cols.append(col)
            values.append(value)
        lower.append(lo)
        upper.append(hi)

    for i, sid in enumerate(targets):
        row([(j, 1) for j in by_shift[sid]] + [(len(pairs) + i, -shifts[sid].minimum)], 0, 0)
    for uid, entries in by_person.items():
        row([(i, 1) for i in entries], -np.inf, personal[uid])
    for key, entries in by_week.items():
        row([(i, 1) for i in entries], -np.inf, weekly[key])
    conflicts = []
    for first, second in combinations(targets, 2):
        check_time(deadline)
        if conflict(shifts[first], shifts[second], data.rules.free_next_day):
            conflicts.append((first, second))
    for uid in by_person:
        check_time(deadline)
        for first, second in conflicts:
            if (uid, first) in indices and (uid, second) in indices:
                row([(indices[uid, first], 1), (indices[uid, second], 1)], -np.inf, 1)
    size = len(pairs) + len(targets)
    matrix = csc_array((np.asarray(values, dtype=float), (rows, cols)), shape=(len(lower), size))
    ratings = {(a.person_id, a.shift_id): a.rating for a in data.availability}
    filled, yellow, preferred = (np.zeros(size) for _ in range(3))
    filled[len(pairs) :] = -1
    for i, pair in enumerate(pairs):
        yellow[i] = ratings[pair] == "if_needed"
        preferred[i] = -(ratings[pair] == "preferred")
    return Model(
        pairs, targets, LinearConstraint(matrix, lower, upper), (filled, yellow, preferred)
    )


def decode_result(data, model, vector):
    vector = np.asarray(vector, dtype=float)
    size = len(model.pairs) + len(model.shifts)
    if (
        vector.shape != (size,)
        or not np.isfinite(vector).all()
        or (np.abs(vector - np.rint(vector)) > 1e-6).any()
    ):
        raise ValueError("non_integral_result")
    binary = np.rint(vector)
    if ((binary < 0) | (binary > 1)).any():
        raise ValueError("non_binary_result")
    pairs = tuple(pair for i, pair in enumerate(model.pairs) if binary[i])
    validate_proposal(data, pairs)
    counts = Counter(sid for _uid, sid in pairs)
    if any(
        bool(binary[len(model.pairs) + i]) != bool(counts[sid])
        for i, sid in enumerate(model.shifts)
    ):
        raise ValueError("inconsistent_filled_flags")
    return pairs


def improve_partners(data, assignments, deadline):
    """Deterministic first-improvement replacements/swaps, with at most 10,000 trials."""
    best = tuple(sorted(assignments))
    score = score_plan(data, best)
    available = defaultdict(set)
    for a in data.availability:
        if a.eligible and a.rating in ("available", "if_needed", "preferred"):
            available[a.shift_id].add(a.person_id)
    examined = 0
    while score.partner_repeats and examined < 10_000 and monotonic() < deadline:
        selected = set(best)

        def alternatives(best=best, selected=selected):
            for uid, sid in best:
                for new_uid in sorted(available[sid]):
                    if (new_uid, sid) not in selected:
                        yield selected - {(uid, sid)} | {(new_uid, sid)}
            for (uid, sid), (other_uid, other_sid) in combinations(best, 2):
                if (
                    uid != other_uid
                    and sid != other_sid
                    and other_uid in available[sid]
                    and uid in available[other_sid]
                    and (other_uid, sid) not in selected
                    and (uid, other_sid) not in selected
                ):
                    yield selected - {(uid, sid), (other_uid, other_sid)} | {
                        (other_uid, sid),
                        (uid, other_sid),
                    }

        improved = False
        for candidate in alternatives():
            if examined >= 10_000 or monotonic() >= deadline:
                break
            examined += 1
            candidate = tuple(sorted(candidate))
            candidate_score = score_plan(data, candidate)
            if (
                candidate_score.primary != score.primary
                or candidate_score.partner_repeats >= score.partner_repeats
            ):
                continue
            try:
                validate_proposal(data, candidate)
            except ValueError:
                continue
            best, score, improved = candidate, candidate_score, True
            break
        if not improved:
            break
    return best, examined


def propose_plan(data: PlanningInput) -> PlanningResult:
    started = monotonic()
    diagnostics = {"proven_stages": [], "stages": [], "partner_trials": 0}

    def finish(status, assignments=(), code=None):
        score = None
        unfilled = ()
        if status in (Status.OPTIMAL_PRIMARY, Status.FEASIBLE_TIMEOUT):
            validate_proposal(data, assignments)
            score = score_plan(data, assignments)
            counts = Counter(sid for _uid, sid in assignments)
            unfilled = tuple(
                (s.id, s.minimum - counts[s.id])
                for s in sorted(data.shifts, key=lambda s: s.id)
                if counts[s.id] < s.minimum
            )
        diagnostics.update(elapsed_seconds=monotonic() - started, code=code)
        return PlanningResult(tuple(sorted(assignments)), score, unfilled, status, diagnostics)

    try:
        validate_input(data)
    except ValueError, TypeError, AttributeError:
        return finish(Status.INVALID_INPUT, code="invalid_input")
    # Leave a small part of the same budget for final independent validation.
    deadline = started + data.rules.solver_seconds - min(0.25, data.rules.solver_seconds * 0.05)
    best = None
    best_score = None
    try:
        model = build_model(data, deadline)
        check_time(deadline)
        diagnostics.update(
            model_seconds=monotonic() - started,
            variables=len(model.pairs) + len(model.shifts),
            constraints=model.constraints.A.shape[0],
        )
        if not model.pairs:
            diagnostics["proven_stages"] = list(STAGES)
            return finish(Status.OPTIMAL_PRIMARY, code="no_eligible_assignments")
        constraints = [model.constraints]
        for stage, objective in zip(STAGES, model.objectives, strict=True):
            check_time(deadline)
            stage_start = monotonic()
            result = milp(
                objective,
                integrality=np.ones(len(objective)),
                bounds=Bounds(0, 1),
                constraints=constraints,
                options={"time_limit": deadline - stage_start, "mip_rel_gap": 0.0},
            )
            record = {"name": stage, "seconds": monotonic() - stage_start, "status": result.status}
            for key in ("mip_gap", "mip_dual_bound"):
                value = getattr(result, key, None)
                record[key] = float(value) if value is not None and np.isfinite(value) else None
            diagnostics["stages"].append(record)
            if result.status not in (0, 1):
                return finish(Status.NO_PROPOSAL, code="solver_error")
            vector = getattr(result, "x", None)
            if vector is not None:
                candidate = decode_result(data, model, vector)
                score = score_plan(data, candidate)
                proved = len(diagnostics["proven_stages"])
                if best_score and score.primary[:proved] != best_score.primary[:proved]:
                    raise ValueError("changed_proven_objective")
                if best_score is None or score.primary > best_score.primary:
                    best, best_score = candidate, score
            elif result.status == 0:
                raise ValueError("missing_optimal_vector")
            if result.status == 1:
                break
            # Later objectives may vary before their own stage has been optimized.
            if score.primary[: proved + 1] != best_score.primary[: proved + 1]:
                raise ValueError("regressed_objective")
            diagnostics["proven_stages"].append(stage)
            target = (-(best_score.filled), best_score.yellow, -best_score.preferred)[
                STAGES.index(stage)
            ]
            constraints.append(
                LinearConstraint(csc_array(objective.reshape(1, -1)), target, target)
            )
        if best is not None:
            best, diagnostics["partner_trials"] = improve_partners(data, best, deadline)
    except BudgetExpired:
        diagnostics["code"] = "time_limit"
    except ValueError, TypeError, RuntimeError:
        return finish(Status.NO_PROPOSAL, code="invalid_solver_result")
    if best is None:
        return finish(Status.NO_PROPOSAL, code="time_limit_without_incumbent")
    status = (
        Status.OPTIMAL_PRIMARY
        if len(diagnostics["proven_stages"]) == 3
        else Status.FEASIBLE_TIMEOUT
    )
    return finish(
        status, best, code="primary_proven" if status == Status.OPTIMAL_PRIMARY else "time_limit"
    )
