from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ephios_shift_coordination import optimizer
from ephios_shift_coordination.scheduling import Availability, Commitment, Person
from tests.test_optimizer import problem


def result(status=0, x=None):
    return SimpleNamespace(status=status, x=x, mip_gap=0 if status == 0 else 0.4, mip_dual_bound=-1)


@pytest.mark.parametrize(
    "vector",
    [
        [0.5, 1],
        [float("nan"), 1],
        [float("inf"), 1],
        [-1, 1],
        [2, 1],
        [1],
        [[1, 1]],
        [1, 0],
        [0, 1],
    ],
)
def test_invalid_or_fractional_solver_vectors_never_become_proposals(monkeypatch, vector):
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: result(x=vector))
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert proposal.status == "no_proposal"
    assert proposal.assignments == () and proposal.score is None
    assert proposal.diagnostics["code"] == "invalid_solver_result"


@pytest.mark.parametrize("state", [2, 3, 4, None])
def test_solver_errors_are_not_reported_as_empty_optimal_plans(monkeypatch, state):
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: result(state))
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert proposal.status == "no_proposal" and proposal.score is None


def test_solver_exception_is_contained(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private solver internals")

    monkeypatch.setattr(optimizer, "milp", fail)
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert proposal.status == "no_proposal"
    assert "private solver internals" not in str(proposal)


@pytest.mark.parametrize("vector", [None, [0, 0], [1, 1]])
def test_timeout_distinguishes_missing_empty_and_filled_incumbents(monkeypatch, vector):
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: result(1, vector))
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert proposal.status == ("no_proposal" if vector is None else "feasible_timeout")
    assert proposal.diagnostics["proven_stages"] == []
    if vector is not None:
        assert proposal.score.filled == vector[1]


def test_later_timeout_retains_the_best_valid_incumbent_and_proof(monkeypatch):
    responses = iter([result(0, [1, 0, 1]), result(1, [0, 1, 1])])
    data = problem(days=(5,), people=2, minimum=1)
    data = replace(
        data,
        availability=(Availability(1, 1, "available", True), Availability(2, 1, "if_needed", True)),
    )
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: next(responses))
    proposal = optimizer.propose_plan(data)
    assert proposal.status == "feasible_timeout"
    assert proposal.assignments == ((1, 1),) and proposal.score.yellow == 0
    assert proposal.diagnostics["proven_stages"] == ["filled"]


def test_changed_proven_objective_or_regressing_optimum_is_rejected(monkeypatch):
    for second in ([0, 0, 0], [0, 1, 1]):
        responses = iter([result(0, [1, 0, 1]), result(0, second)])
        data = problem(days=(5,), people=2, minimum=1)
        data = replace(
            data,
            availability=(
                Availability(1, 1, "available", True),
                Availability(2, 1, "if_needed", True),
            ),
        )
        monkeypatch.setattr(
            optimizer, "milp", lambda *args, responses=responses, **kwargs: next(responses)
        )
        proposal = optimizer.propose_plan(data)
        assert proposal.status == "no_proposal"


def test_optimal_without_vector_is_rejected(monkeypatch):
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: result(0))
    assert optimizer.propose_plan(problem()).status == "no_proposal"


def test_small_integral_tolerance_and_three_proven_stages(monkeypatch):
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: result(0, [1 - 1e-8, 1]))
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert proposal.status == "optimal_primary" and proposal.assignments == ((1, 1),)
    assert proposal.diagnostics["proven_stages"] == ["filled", "yellow", "preferred"]


def test_budget_includes_model_build_and_is_shared_by_solver_stages(monkeypatch):
    clock = [0.0]
    limits = []
    monkeypatch.setattr(optimizer, "monotonic", lambda: clock[0])
    original = optimizer.build_model

    def build(*args):
        model = original(*args)
        clock[0] += 2
        return model

    def solve(*args, **kwargs):
        limits.append(kwargs["options"]["time_limit"])
        clock[0] += 4
        return result(0, [1, 1])

    monkeypatch.setattr(optimizer, "build_model", build)
    monkeypatch.setattr(optimizer, "milp", solve)
    proposal = optimizer.propose_plan(problem(days=(5,), people=1, minimum=1))
    assert limits == [7.75, 3.75]
    assert proposal.status == "feasible_timeout"
    assert proposal.diagnostics["proven_stages"] == ["filled", "yellow"]
    assert proposal.diagnostics["elapsed_seconds"] == 10


def test_model_timeout_does_not_fabricate_an_incumbent(monkeypatch):
    def build(*args):
        raise optimizer.BudgetExpired

    monkeypatch.setattr(optimizer, "build_model", build)
    proposal = optimizer.propose_plan(problem())
    assert proposal.status == "no_proposal" and proposal.score is None


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"people": (Person(True, 2, True),)},
        {"people": (Person(1, None, True),)},
        {"people": (Person(1, -1, True),)},
        {"people": (Person(1, 2, True),) * 2},
        {"availability": (Availability(True, 1, "available", True),)},
        {"availability": (Availability(1, 1, "wrong", True),)},
        {"availability": (Availability(1, 1, "available", True),) * 2},
    ],
)
def test_invalid_input_has_explicit_status(change):
    proposal = optimizer.propose_plan(replace(problem(), **change))
    assert proposal.status == "invalid_input" and proposal.score is None


def test_invalid_dates_ids_capacity_timezone_and_rules():
    data = problem()
    first = data.shifts[0]
    cases = [
        replace(data, period=replace(data.period, timezone="Invalid/zone")),
        replace(
            data, period=replace(data.period, start_date=data.period.end_date + timedelta(days=1))
        ),
        replace(data, rules=replace(data.rules, solver_seconds=0)),
        replace(data, shifts=(replace(first, end=first.start),)),
        replace(data, shifts=(replace(first, local_date=first.local_date + timedelta(days=1)),)),
        replace(data, shifts=(replace(first, start=first.start.replace(tzinfo=None)),)),
        replace(data, shifts=(replace(first, event_type_id=2),)),
        replace(data, shifts=(replace(first, maximum=1),)),
        replace(
            data, commitments=(Commitment(1, 99, 1, first.start, first.end, first.local_date),)
        ),
        None,
    ]
    assert all(optimizer.propose_plan(case).status == "invalid_input" for case in cases)


def test_week_and_person_residual_capacity_saturate_at_zero():
    data = problem(days=(7,), people=1, minimum=1)
    target = data.shifts[0]
    baseline = tuple(
        Commitment(
            i,
            1,
            1,
            target.start - timedelta(days=2),
            target.end - timedelta(days=2),
            target.local_date - timedelta(days=2),
        )
        for i in (10, 11, 12)
    )
    for start_date in (data.period.start_date, target.local_date):
        limited = replace(
            data,
            rules=replace(data.rules, free_next_day=False),
            commitments=baseline,
            period=replace(data.period, start_date=start_date),
        )
        proposal = optimizer.propose_plan(limited)
        assert proposal.status == "optimal_primary" and proposal.assignments == ()


def test_intermediate_optimum_may_change_an_objective_not_yet_optimized(monkeypatch):
    # The yellow stage can choose fewer preferred assignments while proving zero yellow.
    # Keep the better earlier incumbent and still proceed to the preference stage.
    responses = iter(
        [
            result(0, [1, 0, 1]),
            result(0, [0, 1, 1]),
            result(0, [1, 0, 1]),
        ]
    )
    data = replace(
        problem(days=(5,), people=2, minimum=1),
        availability=(
            Availability(1, 1, "preferred", True),
            Availability(2, 1, "available", True),
        ),
    )
    monkeypatch.setattr(optimizer, "milp", lambda *args, **kwargs: next(responses))
    proposal = optimizer.propose_plan(data)
    assert proposal.status == "optimal_primary"
    assert proposal.assignments == ((1, 1),)
    assert proposal.diagnostics["proven_stages"] == ["filled", "yellow", "preferred"]
