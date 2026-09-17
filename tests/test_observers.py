"""Members sitting in: no qualification, counted as staff, without working hours."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from ephios.core.models import LocalParticipation
from ephios.core.models.events import PlaceholderParticipation
from ephios.core.models.users import Notification
from icalendar import Calendar

from ephios_shift_coordination.drafts import load_plan, save_draft, validate_draft
from ephios_shift_coordination.models import DraftAssignment, ObserverParticipation
from ephios_shift_coordination.publication import load_publication, publish_plan
from ephios_shift_coordination.surveys import response_is_actionable, save_response
from tests.test_publication import prepare
from tests.test_surveys import answer, open_for
from tests.test_surveys import survey_data as survey_data


@pytest.fixture
def observer_data(survey_data, monkeypatch):
    data = survey_data
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    data.period = open_for(data)
    data.service = data.period.events.first()
    response = data.period.responses.get(user=data.member)
    save_response(
        data.member,
        data.period.pk,
        **{**answer(response), "maximum": 2, "observer_events": [data.service.pk]},
    )
    # The administrator holds no qualification and can only offer to sit in.
    save_response(
        data.admin,
        data.period.pk,
        expected_version=0,
        maximum=1,
        notes="",
        ratings={},
        observer_events=[data.service.pk],
    )
    data.deadline = data.period.deadline
    monkeypatch.setattr("django.utils.timezone.now", lambda: data.deadline)
    return data


def test_everybody_who_sees_the_services_is_invited_and_can_offer_to_sit_in(observer_data):
    data = observer_data
    assert set(data.period.responses.values_list("user__display_name", flat=True)) == {
        "admin",
        "coordinator",
        "member",
    }
    sitting = data.period.responses.get(user=data.admin)
    assert not sitting.offered_shifts.exists() and sitting.submitted_at
    assert list(sitting.observer_events.values_list("pk", flat=True)) == [data.service.pk]


def test_an_invitation_without_offered_shifts_still_needs_an_answer(survey_data):
    from datetime import date

    from ephios.core.models import UserProfile

    data = survey_data
    guest = UserProfile.objects.create(
        email="guest@example.invalid",
        display_name="guest",
        date_of_birth=date(1990, 1, 1),
        is_active=True,
    )
    guest.groups.add(data.group)
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    period = open_for(data)
    waiting = period.responses.get(user=guest)
    assert not waiting.offered_shifts.exists()
    assert response_is_actionable(waiting)
    guest.groups.clear()
    assert not response_is_actionable(period.responses.get(user=guest))


def test_sitting_in_staffs_a_shift_but_never_without_a_regular_person(observer_data):
    data = observer_data
    shift = data.shifts[0].pk
    plan = load_plan(data.coordinator, data.period.pk)
    assert [data.admin.pk, shift] in plan["observers"]
    assert plan["allow_observers"] is True
    basis = {"expected_version": plan["version"], "fingerprint": plan["fingerprint"]}
    only_sitting = validate_draft(
        data.coordinator,
        data.period.pk,
        **basis,
        assignments=[],
        observers=[[data.admin.pk, shift]],
    )
    assert {v["code"] for v in only_sitting["violations"]} == {"regular_minimum"}
    balanced = validate_draft(
        data.coordinator,
        data.period.pk,
        **basis,
        assignments=[[data.member.pk, shift]],
        observers=[[data.admin.pk, shift]],
    )
    assert not balanced["violations"]
    assert all(row[0] != shift for row in balanced["underfilled"])
    assert balanced["counts"][data.admin.pk]["draft"] == 1


def test_sitting_in_without_an_offer_needs_an_exception(observer_data):
    data = observer_data
    shift = data.shifts[1].pk  # the second shift of another day was never offered
    other = data.period.events.last().shifts.order_by("pk").first().pk
    plan = load_plan(data.coordinator, data.period.pk)
    result = validate_draft(
        data.coordinator,
        data.period.pk,
        expected_version=plan["version"],
        fingerprint=plan["fingerprint"],
        assignments=[[data.member.pk, shift]],
        observers=[[data.admin.pk, other]],
    )
    assert "observer_wish" in {v["code"] for v in result["violations"]}


def test_saved_drafts_keep_both_roles(observer_data):
    data = observer_data
    shift = data.shifts[0].pk
    plan = load_plan(data.coordinator, data.period.pk)
    save_draft(
        data.coordinator,
        data.period.pk,
        expected_version=plan["version"],
        fingerprint=plan["fingerprint"],
        assignments=[[data.member.pk, shift]],
        observers=[[data.admin.pk, shift]],
        confirmations=[],
    )
    assert DraftAssignment.objects.filter(observer=True).count() == 1
    reloaded = load_plan(data.admin, data.period.pk)
    assert reloaded["assignments"] == [[data.member.pk, shift]]
    assert reloaded["observer_assignments"] == [[data.admin.pk, shift]]


def test_sitting_in_is_rejected_when_the_period_does_not_allow_it(observer_data):
    data = observer_data
    data.period.rules = {**data.period.rules, "allow_observers": False}
    data.period.save()
    plan = load_plan(data.coordinator, data.period.pk)
    with pytest.raises(ValidationError):
        save_draft(
            data.coordinator,
            data.period.pk,
            expected_version=plan["version"],
            fingerprint=plan["fingerprint"],
            assignments=[[data.member.pk, data.shifts[0].pk]],
            observers=[[data.admin.pk, data.shifts[0].pk]],
            confirmations=[],
        )


def test_publication_records_sitting_in_without_participation_hours(observer_data, client):
    from django.urls import reverse

    data = observer_data
    shift = data.shifts[0].pk
    body = prepare(data, [[data.member.pk, shift]], observers=[[data.admin.pk, shift]])
    published = publish_plan(data.coordinator, data.period.pk, **body)
    assert LocalParticipation.objects.count() == 1
    placeholder = PlaceholderParticipation.objects.get()
    assert str(data.admin) in placeholder.display_name
    assert placeholder.state == LocalParticipation.States.CONFIRMED
    assert ObserverParticipation.objects.get().user == data.admin
    # ephios counts working hours from confirmed participations of a user, so sitting in earns none.
    assert data.admin.get_workhour_items()[0] == timedelta()
    assert data.member.get_workhour_items()[0] > timedelta()
    feed = reverse("core:user_event_feed", args=[data.admin.calendar_token])
    assert not Calendar.from_ical(client.get(feed).content).walk("VEVENT")
    assert Notification.objects.filter(
        user=data.admin, slug="shift_coordination_published"
    ).exists()
    record = published.publication_snapshot["shifts"][0]["members"]
    assert {member["observer"] for member in record} == {True, False}
    assert not load_publication(data.coordinator, data.period.pk)["changed"]


def test_members_can_tick_sitting_in_in_the_survey(survey_data, client):
    data = survey_data
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    period = open_for(data)
    service = period.events.first()
    response = period.responses.get(user=data.member)
    client.force_login(data.member)
    page = client.get(response.get_absolute_url())
    assert page.status_code == 200
    assert f'name="observe_{service.pk}"'.encode() in page.content
    assert b"sit in" in page.content
    payload = {
        "expected_version": 0,
        "maximum": 2,
        "notes": "",
        f"observe_{service.pk}": "on",
        **{f"rating_{shift.pk}": "available" for shift in response.offered_shifts.all()},
    }
    assert client.post(response.get_absolute_url(), payload).status_code == 302
    assert list(response.observer_events.values_list("pk", flat=True)) == [service.pk]
