from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from ephios.core.models import Event, UserProfile

from ephios_shift_coordination.models import PlanningPeriod
from ephios_shift_coordination.services import create_period
from tests.test_periods import request_data

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]


def test_parallel_creation_commits_exactly_one_series(planning_data):
    assert connection.vendor == "postgresql"
    barrier = Barrier(2)
    arguments = request_data(planning_data)
    user_id = planning_data.coordinator.pk

    def submit():
        close_old_connections()
        try:
            user = UserProfile.objects.get(pk=user_id)
            barrier.wait(timeout=10)
            return create_period(user, **arguments).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: submit(), range(2)))
    assert first == second
    assert PlanningPeriod.objects.count() == 1
    assert Event.objects.count() == 2


def survey_fixture(data):
    from ephios.core.models import QualificationGrant

    period = create_period(data.coordinator, **request_data(data))
    QualificationGrant.objects.create(
        user=data.member, qualification=data.template.shifts.first().qualifications.first()
    )
    return period


def parallel_calls(first, second):
    barrier = Barrier(2)

    def submit(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return action()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(submit, [first, second]))


def test_parallel_opening_and_reminders_create_one_notification_each(planning_data, monkeypatch):
    from datetime import timedelta

    from ephios.core.models.users import Notification

    from ephios_shift_coordination.models import NotificationDispatch
    from ephios_shift_coordination.surveys import open_survey, process_surveys

    period = survey_fixture(planning_data)

    def open_once():
        return open_survey(planning_data.coordinator, period.pk, expected_version=1).pk

    assert parallel_calls(open_once, open_once) == [period.pk, period.pk]
    assert Notification.objects.count() == NotificationDispatch.objects.count() == 1
    period.refresh_from_db()
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=2))
    parallel_calls(process_surveys, process_surveys)
    assert Notification.objects.count() == NotificationDispatch.objects.count() == 2


def test_parallel_responses_detect_version_conflict(planning_data):
    from ephios_shift_coordination.models import SurveyResponse
    from ephios_shift_coordination.services import Conflict
    from ephios_shift_coordination.surveys import open_survey, save_response
    from tests.test_surveys import answer

    period = survey_fixture(planning_data)
    open_survey(planning_data.coordinator, period.pk, expected_version=1)
    response = SurveyResponse.objects.get(period=period)
    payload = answer(response)

    def submit():
        try:
            save_response(planning_data.member, period.pk, **payload)
            return "saved"
        except Conflict:
            return "conflict"

    assert sorted(parallel_calls(submit, submit)) == ["conflict", "saved"]
    response.refresh_from_db()
    assert response.version == 1 and response.availabilities.count() == 2


def test_response_waiting_for_period_lock_rechecks_deadline(planning_data, monkeypatch):
    from datetime import timedelta
    from threading import Event as ThreadEvent

    from django.db import transaction

    from ephios_shift_coordination.models import SurveyResponse
    from ephios_shift_coordination.services import Conflict
    from ephios_shift_coordination.surveys import open_survey, save_response
    from tests.test_surveys import answer

    period = survey_fixture(planning_data)
    open_survey(planning_data.coordinator, period.pk, expected_version=1)
    period.refresh_from_db()
    response = SurveyResponse.objects.get(period=period)
    payload = answer(response)
    clock = [period.deadline - timedelta(seconds=1)]
    monkeypatch.setattr("django.utils.timezone.now", lambda: clock[0])
    started = ThreadEvent()

    def mark_lock_attempt(execute, sql, params, many, context):
        if "FOR UPDATE" in sql and "ephios_shift_coordination_planningperiod" in sql:
            started.set()
        return execute(sql, params, many, context)

    def submit():
        close_old_connections()
        try:
            with connection.execute_wrapper(mark_lock_attempt), pytest.raises(Conflict):
                save_response(planning_data.member, period.pk, **payload)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            PlanningPeriod.objects.select_for_update().get(pk=period.pk)
            future = executor.submit(submit)
            assert started.wait(timeout=10)
            clock[0] = period.deadline
        future.result(timeout=10)
    assert not response.availabilities.exists()


def test_parallel_draft_saves_have_exactly_one_winner(planning_data, monkeypatch):
    from ephios_shift_coordination.drafts import load_plan, save_draft
    from ephios_shift_coordination.models import DraftAssignment
    from ephios_shift_coordination.services import Conflict
    from ephios_shift_coordination.surveys import open_survey, save_response
    from tests.test_surveys import answer

    data = planning_data
    period = survey_fixture(data)
    period = open_survey(data.coordinator, period.pk, expected_version=1)
    response = period.responses.get(user=data.member)
    save_response(data.member, period.pk, **{**answer(response), "maximum": 2})
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline)
    plan = load_plan(data.coordinator, period.pk)
    shifts = list(response.offered_shifts.values_list("pk", flat=True))

    def submit(shift_id):
        try:
            save_draft(
                data.coordinator,
                period.pk,
                expected_version=plan["version"],
                fingerprint=plan["fingerprint"],
                assignments=[[data.member.pk, shift_id]],
                confirmations=[],
            )
            return "saved"
        except Conflict:
            return "conflict"

    assert sorted(parallel_calls(lambda: submit(shifts[0]), lambda: submit(shifts[1]))) == [
        "conflict",
        "saved",
    ]
    assert DraftAssignment.objects.filter(period=period).count() == 1
    period.refresh_from_db()
    assert period.version == plan["version"] + 1
