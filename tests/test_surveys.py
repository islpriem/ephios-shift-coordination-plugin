from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from ephios.core.models import QualificationGrant
from ephios.core.models.users import Notification

from ephios_shift_coordination.models import Availability, NotificationDispatch, SurveyResponse
from ephios_shift_coordination.services import Conflict, create_period
from ephios_shift_coordination.surveys import open_survey, process_surveys, save_response
from tests.test_periods import request_data


@pytest.fixture
def survey_data(planning_data):
    data = planning_data
    data.period = create_period(data.coordinator, **request_data(data))
    data.shifts = list(data.period.events.first().shifts.order_by("pk"))
    for person in (data.member, data.coordinator, data.outsider):
        QualificationGrant.objects.create(
            user=person, qualification=data.template.shifts.first().qualifications.first()
        )
    return data


def open_for(data, **kwargs):
    return open_survey(data.coordinator, data.period.pk, expected_version=1, **kwargs)


def answer(response, **kwargs):
    return dict(
        expected_version=response.version,
        maximum=0,
        notes="<script>private answer</script>",
        ratings={
            pk: Availability.Rating.PREFERRED
            for pk in response.offered_shifts.values_list("pk", flat=True)
        },
        **kwargs,
    )


def test_open_freezes_eligible_cohort_and_creates_one_invitation_per_person(survey_data):
    data = survey_data
    assert not Notification.objects.exists()
    period = open_for(data)
    assert period.deadline == timezone.now() + timedelta(days=7)
    assert period.responses.count() == 2
    response = period.responses.get(user=data.member)
    assert response.offered_shifts.count() == 2
    assert not response.availabilities.exists()
    assert NotificationDispatch.objects.count() == Notification.objects.count() == 2
    assert open_for(data).deadline == period.deadline
    assert Notification.objects.count() == 2
    QualificationGrant.objects.create(
        user=data.admin, qualification=data.template.shifts.first().qualifications.first()
    )
    assert open_for(data).responses.count() == 2


def test_complete_answer_with_zero_maximum_and_version_conflict(survey_data):
    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    saved = save_response(data.member, data.period.pk, **answer(response))
    assert saved.maximum == 0 and saved.submitted_at and saved.version == 1
    assert saved.availabilities.count() == 2
    with pytest.raises(Conflict):
        save_response(data.member, data.period.pk, **answer(response))
    assert SurveyResponse.objects.get(pk=saved.pk).notes == "<script>private answer</script>"


@pytest.mark.parametrize("offset", [0, 1])
def test_deadline_rejects_answers_without_scheduler(survey_data, monkeypatch, offset):
    data = survey_data
    period = open_for(data)
    response = period.responses.get(user=data.member)
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: period.deadline + timedelta(seconds=offset)
    )
    with pytest.raises(Conflict):
        save_response(data.member, period.pk, **answer(response))
    assert not response.availabilities.exists()


def test_reminders_are_deduplicated_and_complete_answers_are_excluded(survey_data, monkeypatch):
    data = survey_data
    period = open_for(data, reminder_days=[5, 3, 1])
    response = period.responses.get(user=data.member)
    save_response(data.member, period.pk, **answer(response))
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=2))
    process_surveys()
    process_surveys()
    dispatches = NotificationDispatch.objects.filter(kind="reminder")
    assert dispatches.filter(skipped=False).count() == 1
    assert dispatches.filter(skipped=True).count() == 3
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline)
    process_surveys()
    period.refresh_from_db()
    assert period.state == period.State.PLANNING
    assert dispatches.filter(skipped=False).count() == 1


def test_included_and_type_qualifications_must_last_through_shift_end(survey_data):
    from ephios.core.dynamic_preferences_registry import GeneralRequiredQualificationPreference
    from ephios.core.models import Qualification

    from ephios_shift_coordination.ephios_integration import eligible

    data = survey_data
    skill = data.template.shifts.first().qualifications.first()
    parent = Qualification.objects.create(
        title="Supervisor", abbreviation="S", category=skill.category
    )
    parent.includes.add(skill)
    data.member.qualification_grants.all().delete()
    end = data.shifts[0].shift.end_time
    grant = QualificationGrant.objects.create(user=data.member, qualification=parent, expires=end)
    assert eligible(data.member, data.shifts[0])
    grant.expires = end - timedelta(microseconds=1)
    grant.save()
    assert not eligible(data.member, data.shifts[0])
    grant.expires = None
    grant.save()
    assert eligible(data.member, data.shifts[0])
    other = data.template.shifts.last().qualifications.first()
    event_type = data.template.event_type
    event_type.preferences[GeneralRequiredQualificationPreference.name] = [other]
    assert not eligible(data.member, data.shifts[0])
    parent.includes.add(other)
    assert eligible(data.member, data.shifts[0])
    data.member.is_active = False
    data.member.save()
    assert not eligible(data.member, data.shifts[0])


@pytest.mark.parametrize(
    "change",
    [
        {"maximum": -1},
        {"maximum": True},
        {"maximum": 1.5},
        {"notes": "x" * 4001},
        {"ratings": {}},
        {"ratings": {999999: "available"}},
    ],
)
def test_invalid_answer_is_atomic(survey_data, change):
    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    with pytest.raises(ValidationError):
        save_response(data.member, data.period.pk, **{**answer(response), **change})
    response.refresh_from_db()
    assert response.version == 0 and response.maximum is None
    assert not response.availabilities.exists()


def test_unknown_rating_and_missing_invitation_are_rejected(survey_data):
    from django.http import Http404

    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    payload = answer(response)
    payload["ratings"] = {pk: "yes" for pk in payload["ratings"]}
    with pytest.raises(ValidationError):
        save_response(data.member, data.period.pk, **payload)
    with pytest.raises(Http404):
        save_response(data.outsider, data.period.pk, **answer(response))


def test_revoked_access_and_disabled_plugin_are_rechecked(survey_data):
    from dynamic_preferences.registries import global_preferences_registry
    from guardian.shortcuts import remove_perm

    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    data.member.groups.clear()
    with pytest.raises(PermissionDenied):
        save_response(data.member, data.period.pk, **answer(response))
    remove_perm("core.change_event", data.coordination, data.shifts[0].shift.event)
    remove_perm("core.change_event", data.coordinator, data.shifts[0].shift.event)
    with pytest.raises(PermissionDenied):
        open_for(data)
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        p for p in preferences["general__enabled_plugins"] if p != "ephios_shift_coordination"
    ]
    with pytest.raises(PermissionDenied):
        save_response(data.member, data.period.pk, **answer(response))
    with pytest.raises(PermissionDenied):
        open_for(data)
    process_surveys()
    assert Notification.objects.count() == 2


@pytest.mark.parametrize(
    "mutation", ["delete_event", "delete_shift", "time", "type", "qualification", "participation"]
)
def test_native_structure_drift_blocks_writes(survey_data, mutation):
    from ephios.core.dynamic_preferences_registry import GeneralRequiredQualificationPreference
    from ephios.core.models import EventType, LocalParticipation

    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    shift = data.shifts[0].shift
    if mutation == "delete_event":
        shift.event.delete()
    elif mutation == "delete_shift":
        shift.delete()
    elif mutation == "time":
        shift.start_time += timedelta(minutes=10)
        shift.save()
    elif mutation == "type":
        shift.event.type = EventType.objects.create(title="Changed")
        shift.event.save()
    elif mutation == "qualification":
        shift.event.type.preferences[GeneralRequiredQualificationPreference.name] = [
            data.template.shifts.first().qualifications.first()
        ]
    else:
        LocalParticipation.objects.create(
            user=data.member, shift=shift, state=LocalParticipation.States.CONFIRMED
        )
    with pytest.raises(Conflict):
        save_response(data.member, data.period.pk, **answer(response))
    assert not response.availabilities.exists()


def test_configuration_can_be_saved_before_opening_and_stale_versions_fail(survey_data):
    data = survey_data
    deadline = timezone.now() + timedelta(days=9)
    saved = open_for(data, deadline=deadline, reminder_days=[], open_now=False)
    assert saved.deadline == deadline and not saved.opened_at and not Notification.objects.exists()
    with pytest.raises(Conflict):
        open_for(data)
    opened = open_survey(data.coordinator, saved.pk, expected_version=saved.version)
    assert opened.deadline == deadline and opened.rules["reminder_days"] == []
    with pytest.raises(Conflict):
        open_survey(data.coordinator, saved.pk, expected_version=opened.version, open_now=False)


@pytest.mark.parametrize("days", [0, -1, 40])
def test_opening_requires_valid_deadline(survey_data, days):
    with pytest.raises(ValidationError):
        open_for(survey_data, deadline=timezone.now() + timedelta(days=days))
    assert not SurveyResponse.objects.exists()


def test_open_and_answer_roll_back_on_notification_or_availability_failure(
    survey_data, monkeypatch
):
    from unittest.mock import patch

    data = survey_data
    with (
        patch.object(Notification.objects, "create", side_effect=RuntimeError),
        pytest.raises(RuntimeError),
    ):
        open_for(data)
    assert not SurveyResponse.objects.exists() and not NotificationDispatch.objects.exists()
    period = open_for(data)
    response = period.responses.get(user=data.member)
    with (
        patch.object(Availability.objects, "bulk_create", side_effect=RuntimeError),
        pytest.raises(RuntimeError),
    ):
        save_response(data.member, period.pk, **answer(response))
    response.refresh_from_db()
    assert response.version == 0 and response.submitted_at is None


def test_native_notifications_respect_preferences_and_suppress_obsolete_messages(
    survey_data, monkeypatch, mailoutbox
):
    from ephios.core.services.notifications.backends import EmailNotificationBackend

    from ephios_shift_coordination.notifications import SurveyInvitation, SurveyReminder

    data = survey_data
    period = open_for(data, reminder_days=[5, 3])
    notice = Notification.objects.get(user=data.member)
    assert notice.subject == SurveyInvitation.title
    assert "Preferences are not assignments" in notice.body
    assert f"/surveys/{period.pk}/" in notice.get_actions()[0][1]
    assert not notice.is_obsolete
    data.member.disabled_notifications = [[EmailNotificationBackend.slug, SurveyInvitation.slug]]
    data.member.save()
    EmailNotificationBackend.send_multiple([Notification.objects.get(pk=notice.pk)])
    assert not mailoutbox
    data.member.disabled_notifications = []
    data.member.save()
    EmailNotificationBackend.send_multiple([Notification.objects.get(pk=notice.pk)])
    assert len(mailoutbox) == 1 and data.member.email in mailoutbox[0].to[0]
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=4))
    process_surveys()
    old = Notification.objects.get(user=data.member, slug=SurveyReminder.slug)
    assert not old.is_obsolete
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=2))
    process_surveys()
    assert old.is_obsolete
    latest = Notification.objects.filter(user=data.member, slug=SurveyReminder.slug).latest("pk")
    assert not latest.is_obsolete
    response = period.responses.get(user=data.member)
    save_response(data.member, period.pk, **answer(response))
    assert latest.is_obsolete and notice.is_obsolete
    response.delete()
    assert notice.is_obsolete


def test_real_periodic_command_closes_survey_and_processes_reminders(survey_data, monkeypatch):
    from django.core.management import call_command

    data = survey_data
    period = open_for(data)
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=2))
    call_command("run_periodic")
    call_command("run_periodic")
    assert NotificationDispatch.objects.filter(kind="reminder", skipped=False).count() == 2
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline)
    call_command("run_periodic")
    period.refresh_from_db()
    assert period.state == period.State.PLANNING


def form_payload(response):
    payload = answer(response)
    payload.update({f"rating_{pk}": value for pk, value in payload.pop("ratings").items()})
    return payload


def test_survey_views_keep_answers_private_and_readable_after_eligibility_loss(
    survey_data, client, monkeypatch
):
    from django.urls import reverse

    data = survey_data
    period = open_for(data)
    response = period.responses.get(user=data.member)
    url = response.get_absolute_url()
    client.force_login(data.member)
    assert client.get(url).status_code == 200
    payload = {**form_payload(response), "user_id": data.coordinator.pk}
    assert client.post(url, payload).status_code == 302
    response.refresh_from_db()
    assert response.maximum == 0
    assert period.responses.get(user=data.coordinator).submitted_at is None
    page = client.get(url)
    assert b"&lt;script&gt;private answer&lt;/script&gt;" in page.content
    assert b"<script>private answer</script>" not in page.content
    assert client.post(url, payload).status_code == 409
    client.force_login(data.coordinator)
    assert b"private answer" not in client.get(url).content
    assert b"private answer" in client.get(period.get_absolute_url()).content
    client.force_login(data.outsider)
    assert client.get(url).status_code == 404
    assert client.post(url, payload).status_code == 404
    assert (
        b"private answer"
        not in client.get(reverse("ephios_shift_coordination:survey_list")).content
    )
    client.force_login(data.member)
    data.member.qualification_grants.all().delete()
    assert b"Currently no longer eligible" in client.get(url).content
    payload["expected_version"] = 1
    payload["notes"] = "A complete replacement"
    assert client.post(url, payload).status_code == 302
    response.refresh_from_db()
    assert response.version == 2 and response.notes == "A complete replacement"
    data.member.groups.clear()
    page = client.get(url)
    assert page.status_code == 200 and b"Your response is read-only." in page.content
    assert client.post(url, {**payload, "expected_version": 2}).status_code == 403
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline)
    page = client.get(url)
    assert b"Planning" in page.content and b"Your response is read-only." in page.content
    assert client.post(url, {**payload, "expected_version": 2}).status_code == 409


def test_survey_endpoints_reject_csrf_methods_missing_fields_and_extra_shift_ids(
    survey_data, client
):
    from django.test import Client
    from django.urls import reverse
    from dynamic_preferences.registries import global_preferences_registry

    data = survey_data
    period = open_for(data)
    response = period.responses.get(user=data.member)
    url = response.get_absolute_url()
    assert client.get(url).status_code == 302
    client.force_login(data.member)
    assert client.put(url).status_code == 405
    opening = reverse("ephios_shift_coordination:survey_open", args=[period.pk])
    assert client.post(opening).status_code == 403
    assert client.post(url, {}).status_code == 200
    assert (
        client.post(url, {**form_payload(response), "rating_999999": "available"}).status_code
        == 200
    )
    assert not response.availabilities.exists()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(data.member)
    assert csrf_client.post(url, form_payload(response)).status_code == 403
    client.force_login(data.coordinator)
    assert client.get(opening).status_code == 405
    assert csrf_client.post(opening, {}).status_code == 403
    data.member.is_active = False
    data.member.save()
    client.force_login(data.member)
    assert client.get(url).status_code in (302, 403)
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        p for p in preferences["general__enabled_plugins"] if p != "ephios_shift_coordination"
    ]
    assert client.get(url).status_code == 404


def test_opening_form_can_save_config_and_open_only_once(survey_data, client):
    from django.urls import reverse

    data = survey_data
    client.force_login(data.coordinator)
    opening = reverse("ephios_shift_coordination:survey_open", args=[data.period.pk])
    payload = {
        "expected_version": 1,
        "deadline": "2026-09-08T12:00",
        "reminder_days": "3",
        "action": "save_settings",
    }
    assert client.post(opening, payload).status_code == 302
    assert not Notification.objects.exists()
    assert client.post(opening, {**payload, "action": "open"}).status_code == 409
    payload.update(expected_version=2, action="open")
    assert client.post(opening, {**payload, "deadline": "2026-08-01T12:00"}).status_code == 200
    assert client.post(opening, {**payload, "reminder_days": "3,3"}).status_code == 200
    assert client.post(opening, {**payload, "reminder_days": "bad"}).status_code == 200
    assert client.post(opening, payload).status_code == 302
    assert client.post(opening, payload).status_code == 302
    assert Notification.objects.count() == 2
    client.force_login(data.member)
    assert b"Open your survey" in client.get(data.shifts[0].shift.event.get_absolute_url()).content
    assert b"Response saved" not in client.get(data.period.get_absolute_url()).content
    assert client.get(data.period.get_absolute_url()).status_code == 403


def test_delete_native_target_preserves_own_response_history(survey_data, client):
    data = survey_data
    open_for(data)
    response = SurveyResponse.objects.get(user=data.member)
    save_response(data.member, data.period.pk, **answer(response))
    data.shifts[0].shift.event.delete()
    client.force_login(data.member)
    page = client.get(response.get_absolute_url())
    assert page.status_code == 200 and b"private answer" in page.content
    data.member.delete()
    assert not SurveyResponse.objects.filter(pk=response.pk).exists()


def test_survey_lists_shifts_chronologically_even_if_template_order_differs(planning_data):
    from ephios_shift_coordination.forms import SurveyResponseForm

    data = planning_data
    first = data.template.shifts.first()
    first.position = 99
    first.save()
    for shift in data.template.shifts.all():
        QualificationGrant.objects.create(
            user=data.member, qualification=shift.qualifications.first()
        )
    period = create_period(data.coordinator, **request_data(data))
    open_survey(data.coordinator, period.pk, expected_version=1)
    form = SurveyResponseForm(response=period.responses.get(user=data.member))
    starts = [shift.shift.start_time for shift in form.offered]
    assert starts == sorted(starts)


def test_invitation_deadline_uses_the_period_timezone(survey_data):
    from zoneinfo import ZoneInfo

    from django.utils.formats import date_format

    period = open_for(survey_data)
    notice = Notification.objects.get(user=survey_data.member)
    local_deadline = timezone.localtime(period.deadline, ZoneInfo(period.timezone))
    assert date_format(local_deadline, "DATETIME_FORMAT") in notice.body
