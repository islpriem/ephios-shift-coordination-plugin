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

from .scheduling import PlanningInput, service_key, shared_event, validate, week_start

# "sitting" counts people sitting in: a shift is better served by regular staff, so the
# stage only exists when sitting in is possible and plans without it keep the old model.
STAGES = ("filled", "sitting", "yellow", "preferred")


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
    sitting: int = 0

    @property
    def primary(self):
        return self.filled, -self.yellow, self.preferred

    def ranking(self, stages=STAGES):
        values = {
            "filled": self.filled,
            "sitting": -self.sitting,
            "yellow": -self.yellow,
            "preferred": self.preferred,
        }
        return tuple(values[stage] for stage in stages)


@dataclass(frozen=True)
class PlanningResult:
    assignments: tuple[tuple[int, int], ...]
    score: Score | None
    unfilled: tuple[tuple[int, int], ...]
    status: Status
    diagnostics: dict
    schema_version: int = 1
    observers: tuple[tuple[int, int], ...] = ()


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
        or not integer(rules.minimum_regular, 1)
        or type(rules.free_next_day) is not bool
        or type(rules.allow_observers) is not bool
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
            or (entry.event_id is not None and not integer(entry.event_id, 1))
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
        not integer(c.person_id, 1) or c.person_id not in people or type(c.observer) is not bool
        for c in data.commitments
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
    wished = set()
    for o in data.observers:
        key = o.person_id, o.shift_id
        if (
            key in wished
            or not integer(o.person_id, 1)
            or not integer(o.shift_id, 1)
            or o.person_id not in people
            or o.shift_id not in shifts
        ):
            raise ValueError("observer")
        wished.add(key)


def validate_proposal(data, assignments, observers=()):
    """Check business rules and exactly-minimum-or-zero, independently of MILP rows."""
    if observers and not data.rules.allow_observers:
        raise ValueError("observers_disabled")
    result = validate(data, assignments, observers)
    counts = Counter(sid for _uid, sid in (*assignments, *observers))
    if result.violations or any(counts[s.id] not in (0, s.minimum) for s in data.shifts):
        raise ValueError("invalid_proposal")
    return result


def score_plan(data, assignments, observers=()):
    ratings = {(a.person_id, a.shift_id): a.rating for a in data.availability}
    teams = defaultdict(list)
    for uid, sid in (*assignments, *observers):
        teams[sid].append(uid)
    partners = Counter(pair for team in teams.values() for pair in combinations(sorted(team), 2))
    return Score(
        len(teams),
        sum(ratings[pair] == "if_needed" for pair in assignments),
        sum(ratings[pair] == "preferred" for pair in assignments),
        sum(max(0, count - 1) for count in partners.values()),
        len(tuple(observers)),
    )


def conflict(first, second, free_next_day, first_observer=False, second_observer=False):
    if (first_observer or second_observer) and shared_event(first, second):
        return False
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
    objectives: dict
    observers: tuple = ()
    credits: tuple = ()


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
        serving = {service_key(c) for c in baseline[p.id] if not c.observer}
        load = [c for c in baseline[p.id] if not c.observer or service_key(c) not in serving]
        personal[p.id] = max(
            0,
            (p.maximum or 0)
            - sum(data.period.start_date <= c.local_date <= data.period.end_date for c in load),
        )
        for week in {week_start(s.local_date) for s in data.shifts}:
            weekly[p.id, week] = max(
                0,
                data.rules.weekly_limit - sum(week_start(c.local_date) == week for c in load),
            )

    committed = {
        (c.person_id, service_key(c))
        for c in data.commitments
        if c.event_type_id == data.period.event_type_id and not c.observer
    }

    def has_capacity(person, shift, free=False, observer=False):
        # Sitting in beside an own regular duty at that event needs no extra capacity.
        return (
            person.complete
            and (free or (personal[person.id] and weekly[person.id, week_start(shift.local_date)]))
            and not any(
                conflict(shift, c, data.rules.free_next_day, observer, c.observer)
                for c in baseline[person.id]
            )
        )

    candidates = []
    for answer in data.availability:
        check_time(deadline)
        p, s = people[answer.person_id], shifts[answer.shift_id]
        if (
            answer.eligible
            and answer.rating in ("available", "if_needed", "preferred")
            and has_capacity(p, s)
        ):
            candidates.append((p.id, s.id))
    pairs = tuple(sorted(candidates))
    ratings = {(a.person_id, a.shift_id): a.rating for a in data.availability}
    # Sitting in is offered where somebody asked for it and on every service the person
    # could serve anyway: sibling shifts need no separate offer.
    watching = set()
    if data.rules.allow_observers:
        possible = {
            (uid, other.id)
            for uid, sid in pairs
            for other in data.shifts
            if service_key(other) == service_key(shifts[sid]) and other.id != sid
        }
        possible |= {
            (uid, shift.id)
            for uid, key in committed
            for shift in data.shifts
            if service_key(shift) == key
        }
        for uid, sid in sorted(possible | {(o.person_id, o.shift_id) for o in data.observers}):
            check_time(deadline)
            if ratings.get((uid, sid)) == "unavailable":
                continue
            free = (uid, sid) in possible
            if has_capacity(people[uid], shifts[sid], free, observer=True):
                watching.add((uid, sid))
    observers = tuple(sorted(watching))
    # Sitting in beside an existing regular commitment at that event needs no offer.
    free_pairs = {
        pair for pair in observers if (pair[0], service_key(shifts[pair[1]])) in committed
    }
    wished = {(o.person_id, o.shift_id) for o in data.observers} | free_pairs
    targets = tuple(sorted(shifts))
    filled_offset = len(pairs)
    observer_offset = len(pairs) + len(targets)
    double_offset = observer_offset + len(observers)
    regular_index = {pair: i for i, pair in enumerate(pairs)}
    observer_index = {pair: observer_offset + i for i, pair in enumerate(observers)}
    by_shift, by_person, by_week, by_pair = (defaultdict(list) for _ in range(4))
    served = defaultdict(list)
    for index, sitting in ((regular_index, False), (observer_index, True)):
        for (uid, sid), column in index.items():
            by_shift[sid].append(column)
            by_person[uid].append(column)
            by_week[uid, week_start(shifts[sid].local_date)].append(column)
            by_pair[uid, sid].append(column)
            if not sitting:
                served[uid, service_key(shifts[sid])].append((sid, column))
    # Columns that never cost capacity: sitting in beside an existing commitment, and every
    # column that may only be used together with an own regular duty at that event.
    free_columns = {observer_index[pair] for pair in free_pairs}
    free_columns |= {observer_index[pair] for pair in observers if pair not in wished}
    # A credit column is only needed where an offered shift may be taken either way: it
    # cancels the load again when the person serves that event.
    credits, credit = [], {}
    for uid, sid in observers:
        others = [c for other, c in served[uid, service_key(shifts[sid])] if other != sid]
        if others and observer_index[uid, sid] not in free_columns:
            credit[uid, sid] = (double_offset + len(credits), others)
            credits.append((uid, sid))
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
        row([(j, 1) for j in by_shift[sid]] + [(filled_offset + i, -shifts[sid].minimum)], 0, 0)

    # Free columns carry no load at all; credited ones are subtracted again when they fall
    # at an event the person serves anyway.
    def load(entries, uid, week=None):
        terms = [(column, 1) for column in entries if column not in free_columns]
        return terms + [
            (credit[pair][0], -1)
            for pair in credits
            if pair[0] == uid and (week is None or week_start(shifts[pair[1]].local_date) == week)
        ]

    for uid, entries in by_person.items():
        row(load(entries, uid), -np.inf, personal[uid])
    for (uid, week), entries in by_week.items():
        row(load(entries, uid, week), -np.inf, weekly[uid, week])
    conflicts = []
    for first, second in combinations(targets, 2):
        check_time(deadline)
        if conflict(shifts[first], shifts[second], data.rules.free_next_day):
            conflicts.append((first, second))

    for uid in by_person:
        check_time(deadline)
        for first, second in conflicts:
            if shared_event(shifts[first], shifts[second]):
                left = [regular_index[uid, first]] if (uid, first) in regular_index else []
                right = [regular_index[uid, second]] if (uid, second) in regular_index else []
                if left and right:
                    row([(i, 1) for i in left + right], -np.inf, 1)
                continue
            left, right = by_pair[uid, first], by_pair[uid, second]
            if left and right:
                row([(i, 1) for i in left + right], -np.inf, 1)
    watched = {sid for _uid, sid in observers}
    for i, sid in enumerate(targets):
        if sid in watched:
            required = min(data.rules.minimum_regular, shifts[sid].minimum)
            row(
                [(j, 1) for j in by_shift[sid] if j < filled_offset]
                + [(filled_offset + i, -required)],
                0,
                np.inf,
            )
    for uid, sid in observers:
        check_time(deadline)
        column = observer_index[uid, sid]
        others = [c for other, c in served[uid, service_key(shifts[sid])] if other != sid]
        if (uid, sid) in regular_index:
            row([(column, 1), (regular_index[uid, sid], 1)], -np.inf, 1)
        double = credit.get((uid, sid), (None, None))[0]
        if double is not None:
            # The credit only applies where the person really serves that event.
            row([(double, 1), (column, -1)], -np.inf, 0)
            row([(double, 1)] + [(other, -1) for other in others], -np.inf, 0)
        if (uid, sid) not in wished:
            # Without an offer, sitting in requires an own regular duty at that event.
            row([(column, 1)] + [(other, -1) for other in others], -np.inf, 0)
    size = len(pairs) + len(targets) + len(observers) + len(credits)
    matrix = csc_array((np.asarray(values, dtype=float), (rows, cols)), shape=(len(lower), size))
    filled, sitting, yellow, preferred = (np.zeros(size) for _ in range(4))
    filled[filled_offset:observer_offset] = -1
    sitting[observer_offset:double_offset] = 1
    for i, pair in enumerate(pairs):
        yellow[i] = ratings[pair] == "if_needed"
        preferred[i] = -(ratings[pair] == "preferred")
    return Model(
        pairs,
        targets,
        LinearConstraint(matrix, lower, upper),
        {"filled": filled, "sitting": sitting, "yellow": yellow, "preferred": preferred},
        observers,
        tuple(credits),
    )


def decode_result(data, model, vector):
    vector = np.asarray(vector, dtype=float)
    size = len(model.pairs) + len(model.shifts) + len(model.observers) + len(model.credits)
    if (
        vector.shape != (size,)
        or not np.isfinite(vector).all()
        or (np.abs(vector - np.rint(vector)) > 1e-6).any()
    ):
        raise ValueError("non_integral_result")
    binary = np.rint(vector)
    if ((binary < 0) | (binary > 1)).any():
        raise ValueError("non_binary_result")
    offset = len(model.pairs) + len(model.shifts)
    pairs = tuple(pair for i, pair in enumerate(model.pairs) if binary[i])
    observers = tuple(pair for i, pair in enumerate(model.observers) if binary[offset + i])
    validate_proposal(data, pairs, observers)
    counts = Counter(sid for _uid, sid in (*pairs, *observers))
    if any(
        bool(binary[len(model.pairs) + i]) != bool(counts[sid])
        for i, sid in enumerate(model.shifts)
    ):
        raise ValueError("inconsistent_filled_flags")
    return pairs, observers


def improve_partners(data, assignments, deadline, observers=()):
    """Deterministic first-improvement replacements/swaps, with at most 10,000 trials."""
    best = tuple(sorted(assignments))
    score = score_plan(data, best, observers)
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
            candidate_score = score_plan(data, candidate, observers)
            if (
                candidate_score.ranking() != score.ranking()
                or candidate_score.partner_repeats >= score.partner_repeats
            ):
                continue
            try:
                validate_proposal(data, candidate, observers)
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

    def finish(status, assignments=(), observers=(), code=None):
        score = None
        unfilled = ()
        if status in (Status.OPTIMAL_PRIMARY, Status.FEASIBLE_TIMEOUT):
            validate_proposal(data, assignments, observers)
            score = score_plan(data, assignments, observers)
            counts = Counter(sid for _uid, sid in (*assignments, *observers))
            unfilled = tuple(
                (s.id, s.minimum - counts[s.id])
                for s in sorted(data.shifts, key=lambda s: s.id)
                if counts[s.id] < s.minimum
            )
        diagnostics.update(elapsed_seconds=monotonic() - started, code=code)
        return PlanningResult(
            tuple(sorted(assignments)),
            score,
            unfilled,
            status,
            diagnostics,
            observers=tuple(sorted(observers)),
        )

    try:
        validate_input(data)
    except ValueError, TypeError, AttributeError:
        return finish(Status.INVALID_INPUT, code="invalid_input")
    # Leave a small part of the same budget for final independent validation.
    deadline = started + data.rules.solver_seconds - min(0.25, data.rules.solver_seconds * 0.05)
    best = None
    best_observers = ()
    best_score = None
    stages = STAGES
    try:
        model = build_model(data, deadline)
        check_time(deadline)
        stages = tuple(stage for stage in STAGES if stage != "sitting" or model.observers)
        diagnostics.update(
            model_seconds=monotonic() - started,
            variables=len(model.pairs)
            + len(model.shifts)
            + len(model.observers)
            + len(model.credits),
            constraints=model.constraints.A.shape[0],
        )
        if not model.pairs:
            # Every filled shift needs at least one regular person, so observers alone cannot help.
            diagnostics["proven_stages"] = list(stages)
            return finish(Status.OPTIMAL_PRIMARY, code="no_eligible_assignments")
        constraints = [model.constraints]
        for stage in stages:
            check_time(deadline)
            objective = model.objectives[stage]
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
                candidate, watching = decode_result(data, model, vector)
                score = score_plan(data, candidate, watching)
                proved = len(diagnostics["proven_stages"])
                if (
                    best_score
                    and score.ranking(stages)[:proved] != best_score.ranking(stages)[:proved]
                ):
                    raise ValueError("changed_proven_objective")
                if best_score is None or score.ranking(stages) > best_score.ranking(stages):
                    best, best_observers, best_score = candidate, watching, score
            elif result.status == 0:
                raise ValueError("missing_optimal_vector")
            if result.status == 1:
                break
            # Later objectives may vary before their own stage has been optimized.
            if score.ranking(stages)[: proved + 1] != best_score.ranking(stages)[: proved + 1]:
                raise ValueError("regressed_objective")
            diagnostics["proven_stages"].append(stage)
            target = {
                "filled": -best_score.filled,
                "sitting": best_score.sitting,
                "yellow": best_score.yellow,
                "preferred": -best_score.preferred,
            }[stage]
            constraints.append(
                LinearConstraint(csc_array(objective.reshape(1, -1)), target, target)
            )
        if best is not None:
            best, diagnostics["partner_trials"] = improve_partners(
                data, best, deadline, best_observers
            )
    except BudgetExpired:
        diagnostics["code"] = "time_limit"
    except ValueError, TypeError, RuntimeError:
        return finish(Status.NO_PROPOSAL, code="invalid_solver_result")
    if best is None:
        return finish(Status.NO_PROPOSAL, code="time_limit_without_incumbent")
    status = (
        Status.OPTIMAL_PRIMARY
        if len(diagnostics["proven_stages"]) == len(stages)
        else Status.FEASIBLE_TIMEOUT
    )
    return finish(
        status,
        best,
        best_observers,
        code="primary_proven" if status == Status.OPTIMAL_PRIMARY else "time_limit",
    )
