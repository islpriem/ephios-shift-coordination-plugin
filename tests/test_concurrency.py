from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from ephios.core.models import Event, UserProfile

from ephios_shift_coordination.models import PlanningPeriod
from ephios_shift_coordination.services import create_period
from tests.test_drafts import draft_data as draft_data
from tests.test_periods import request_data
from tests.test_publication import prepare
from tests.test_surveys import survey_data as survey_data

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


def test_proposal_releases_locks_and_detects_a_draft_saved_during_calculation(
    planning_data, monkeypatch
):
    from threading import Event as ThreadEvent

    from ephios_shift_coordination import proposals
    from ephios_shift_coordination.drafts import load_plan, save_draft
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
    loaded, proceed = ThreadEvent(), ThreadEvent()
    original = proposals.propose_plan

    def calculate(pure_input):
        loaded.set()
        assert proceed.wait(timeout=10), "A draft save could not proceed while calculating."
        return original(pure_input)

    monkeypatch.setattr(proposals, "propose_plan", calculate)

    def run_proposal():
        close_old_connections()
        try:
            return proposals.create_proposal(
                data.coordinator,
                period.pk,
                expected_version=plan["version"],
                fingerprint=plan["fingerprint"],
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_proposal)
        assert loaded.wait(timeout=10)
        save_draft(
            data.coordinator,
            period.pk,
            expected_version=plan["version"],
            fingerprint=plan["fingerprint"],
            assignments=[],
            confirmations=[],
        )
        proceed.set()
        with pytest.raises(Conflict):
            future.result(timeout=15)
    period.refresh_from_db()
    assert period.version == plan["version"] + 1


def test_parallel_publications_commit_one_set_of_participations_and_summaries(
    draft_data, monkeypatch
):
    from ephios.core.models import LocalParticipation

    from ephios_shift_coordination import publication
    from ephios_shift_coordination.models import NotificationDispatch

    data = draft_data
    body = prepare(data)
    monkeypatch.setattr(publication, "send_all_notifications", lambda: None)

    def publish():
        return publication.publish_plan(data.coordinator, data.period.pk, **body).version

    assert parallel_calls(publish, publish) == [body["expected_version"] + 1] * 2
    assert LocalParticipation.objects.count() == 1
    assert NotificationDispatch.objects.filter(kind="publication").count() == 1


def test_native_assignment_winning_user_lock_prevents_publication(draft_data, monkeypatch):
    from threading import Event as ThreadEvent

    from django.db import transaction
    from ephios.core.models import LocalParticipation

    from ephios_shift_coordination import publication
    from ephios_shift_coordination.services import Conflict

    data = draft_data
    body = prepare(data)
    attempted = ThreadEvent()
    monkeypatch.setattr(publication, "send_all_notifications", lambda: None)

    def detect(execute, sql, params, many, context):
        if "FOR UPDATE" in sql and "userprofile" in sql.lower():
            attempted.set()
        return execute(sql, params, many, context)

    def publish():
        close_old_connections()
        try:
            with connection.execute_wrapper(detect), pytest.raises(Conflict):
                publication.publish_plan(data.coordinator, data.period.pk, **body)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            UserProfile.objects.select_for_update().get(pk=data.member.pk)
            future = executor.submit(publish)
            assert attempted.wait(timeout=10)
            LocalParticipation.objects.create(
                user=data.member,
                shift=data.shifts[0].shift,
                state=LocalParticipation.States.CONFIRMED,
            )
        future.result(timeout=15)
    data.period.refresh_from_db()
    assert data.period.state == PlanningPeriod.State.PLANNING
    assert LocalParticipation.objects.count() == 1


def test_publication_locks_current_grants_until_the_native_writes_commit(draft_data, monkeypatch):
    from threading import Event as ThreadEvent

    from django.db import OperationalError, transaction
    from ephios.core.models import LocalParticipation, QualificationGrant

    from ephios_shift_coordination import publication

    data = draft_data
    body = prepare(data)
    checked, proceed = ThreadEvent(), ThreadEvent()
    original = LocalParticipation.save
    monkeypatch.setattr(publication, "send_all_notifications", lambda: None)

    def pause(self, *args, **kwargs):
        checked.set()
        assert proceed.wait(timeout=10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LocalParticipation, "save", pause)

    def publish():
        close_old_connections()
        try:
            return publication.publish_plan(data.coordinator, data.period.pk, **body).version
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(publish)
        assert checked.wait(timeout=10)
        try:
            # An existing grant update must wait too, not just a user/shift or new FK insert.
            with pytest.raises(OperationalError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '300ms'")
                QualificationGrant.objects.filter(user=data.member).update(
                    expires=data.period.deadline
                )
        finally:
            proceed.set()
        assert future.result(timeout=15) == body["expected_version"] + 1


def test_publication_rechecks_actor_after_waiting_for_user_locks(draft_data, monkeypatch):
    from threading import Event as ThreadEvent

    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from ephios.core.models import LocalParticipation

    from ephios_shift_coordination import publication

    data = draft_data
    body = prepare(data)
    attempted = ThreadEvent()
    monkeypatch.setattr(publication, "send_all_notifications", lambda: None)

    def detect(execute, sql, params, many, context):
        if "FOR UPDATE" in sql and "userprofile" in sql.lower():
            attempted.set()
        return execute(sql, params, many, context)

    def publish():
        close_old_connections()
        try:
            with connection.execute_wrapper(detect), pytest.raises(PermissionDenied):
                publication.publish_plan(data.coordinator, data.period.pk, **body)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            UserProfile.objects.select_for_update().get(pk=data.coordinator.pk)
            future = executor.submit(publish)
            assert attempted.wait(timeout=10)
            UserProfile.objects.filter(pk=data.coordinator.pk).update(is_active=False)
        future.result(timeout=15)
    assert not LocalParticipation.objects.exists()


@pytest.mark.parametrize("change", ["state", "times"])
def test_publication_locks_existing_native_scheduling_inputs(draft_data, monkeypatch, change):
    from datetime import timedelta
    from threading import Event as ThreadEvent

    from django.db import OperationalError, transaction
    from ephios.core.models import LocalParticipation, Shift

    from ephios_shift_coordination import publication
    from tests.test_drafts import native_commitment

    data = draft_data
    existing = native_commitment(data)
    if change == "state":
        LocalParticipation.objects.filter(pk=existing.pk).update(
            state=LocalParticipation.States.REQUESTED
        )
    else:
        Shift.objects.filter(pk=existing.shift_id).update(
            start_time=existing.shift.start_time + timedelta(days=365),
            end_time=existing.shift.end_time + timedelta(days=365),
        )
    body = prepare(data)
    checked, proceed = ThreadEvent(), ThreadEvent()
    original = LocalParticipation.save
    monkeypatch.setattr(publication, "send_all_notifications", lambda: None)

    def pause(self, *args, **kwargs):
        checked.set()
        assert proceed.wait(timeout=10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LocalParticipation, "save", pause)

    def publish():
        close_old_connections()
        try:
            return publication.publish_plan(data.coordinator, data.period.pk, **body).version
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(publish)
        assert checked.wait(timeout=10)
        try:
            with pytest.raises(OperationalError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '300ms'")
                if change == "state":
                    LocalParticipation.objects.filter(pk=existing.pk).update(
                        state=LocalParticipation.States.CONFIRMED
                    )
                else:
                    Shift.objects.filter(pk=existing.shift_id).update(
                        start_time=data.shifts[0].shift.start_time,
                        end_time=data.shifts[0].shift.end_time,
                    )
        finally:
            proceed.set()
        assert future.result(timeout=15) == body["expected_version"] + 1
