"""The three public questions a display at the station may ask."""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from ephios.core.models import AbstractParticipation, Event, LocalParticipation, Shift

from ephios_shift_coordination.models import PlanningSettings

NAMESPACE = "ephios_shift_coordination:"


def url(name):
    return reverse(NAMESPACE + name)


@pytest.fixture
def clock(monkeypatch):
    def move(moment):
        monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
        return moment

    return move


def duty(planning_data, *, start, hours=4, people=(), minimum=1, event_type=None):
    event = Event.objects.create(
        title="Duty",
        location="Test location",
        type=event_type or planning_data.template.event_type,
        active=True,
    )
    shift = Shift.objects.create(
        event=event,
        meeting_time=start,
        start_time=start,
        end_time=start + timedelta(hours=hours),
        signup_flow_slug="manual",
        signup_flow_configuration={"no_selfservice_explanation": ""},
        structure_slug="uniform",
        structure_configuration={
            "required_qualification_ids": [],
            "minimum_number_of_participants": minimum,
            "maximum_number_of_participants": None,
        },
    )
    for person in people:
        LocalParticipation.objects.create(
            shift=shift, user=person, state=AbstractParticipation.States.CONFIRMED
        )
    return shift


def switch_on(planning_data, *types):
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    settings.api_enabled = True
    settings.save()
    settings.api_event_types.set(types or [planning_data.template.event_type])
    return settings


@pytest.mark.parametrize("page", ["api_now", "api_next", "api_week"])
@pytest.mark.django_db
def test_the_endpoints_are_switched_off_until_somebody_turns_them_on(planning_data, client, page):
    assert client.get(url(page)).status_code == 404
    switch_on(planning_data)
    assert client.get(url(page)).status_code == 200


@pytest.mark.django_db
def test_duty_now_is_true_only_inside_a_sufficiently_staffed_shift(planning_data, client, clock):
    switch_on(planning_data)
    start = datetime(2026, 9, 2, 9, tzinfo=UTC)
    shift = duty(planning_data, start=start, people=[planning_data.member], minimum=1)
    clock(start - timedelta(hours=1))
    assert client.get(url("api_now")).json() == {"duty": False, "until": None}
    clock(start + timedelta(hours=1))
    answer = client.get(url("api_now")).json()
    assert answer["duty"] is True and answer["until"] == shift.end_time.isoformat()
    clock(shift.end_time)
    assert client.get(url("api_now")).json()["duty"] is False


@pytest.mark.django_db
def test_a_shift_below_its_minimum_is_no_duty(planning_data, client, clock):
    switch_on(planning_data)
    start = datetime(2026, 9, 2, 9, tzinfo=UTC)
    duty(planning_data, start=start, people=[planning_data.member], minimum=2)
    clock(start + timedelta(hours=1))
    assert client.get(url("api_now")).json()["duty"] is False
    assert client.get(url("api_next")).json() == {"start": None}


@pytest.mark.django_db
def test_an_event_type_nobody_selected_is_never_counted(planning_data, client, clock):
    from ephios.core.models import EventType

    other = EventType.objects.create(title="Assembly")
    switch_on(planning_data)
    start = datetime(2026, 9, 2, 9, tzinfo=UTC)
    duty(planning_data, start=start, people=[planning_data.member], event_type=other)
    clock(start + timedelta(hours=1))
    assert client.get(url("api_now")).json()["duty"] is False
    assert client.get(url("api_week")).json()["days"] == [False] * 7


@pytest.mark.django_db
def test_the_next_duty_is_the_first_staffed_one(planning_data, client, clock):
    switch_on(planning_data)
    clock(datetime(2026, 9, 1, tzinfo=UTC))
    duty(planning_data, start=datetime(2026, 9, 3, 9, tzinfo=UTC), minimum=1)
    wanted = duty(
        planning_data,
        start=datetime(2026, 9, 4, 9, tzinfo=UTC),
        people=[planning_data.member],
        minimum=1,
    )
    assert client.get(url("api_next")).json() == {"start": wanted.start_time.isoformat()}


@pytest.mark.django_db
def test_the_week_answers_monday_to_sunday(planning_data, client, clock):
    switch_on(planning_data)
    # Asked on Wednesday about a duty on the Friday of the same week.
    clock(datetime(2026, 9, 2, 12, tzinfo=UTC))
    duty(planning_data, start=datetime(2026, 9, 4, 9, tzinfo=UTC), people=[planning_data.member])
    answer = client.get(url("api_week")).json()
    assert answer["days"] == [False, False, False, False, True, False, False]
    assert answer["from"] == "2026-08-31"
    # A duty in the following week does not belong to this answer.
    duty(planning_data, start=datetime(2026, 9, 8, 9, tzinfo=UTC), people=[planning_data.member])
    assert client.get(url("api_week")).json()["days"].count(True) == 1


@pytest.mark.django_db
def test_the_answers_need_no_session_and_carry_no_personal_data(planning_data, client, clock):
    switch_on(planning_data)
    start = datetime(2026, 9, 2, 9, tzinfo=UTC)
    duty(planning_data, start=start, people=[planning_data.member])
    clock(start + timedelta(hours=1))
    for page in ("api_now", "api_next", "api_week"):
        response = client.get(url(page))
        assert response.status_code == 200 and response["Content-Type"] == "application/json"
        assert "member" not in response.content.decode()
        assert client.post(url(page)).status_code == 405


@pytest.mark.django_db
def test_an_administrator_turns_the_information_on_and_picks_the_types(planning_data, client):
    from .test_planning_views import settings_payload

    client.force_login(planning_data.admin)
    response = client.post(
        reverse(NAMESPACE + "settings"),
        settings_payload(
            planning_data.configuration,
            api_enabled="on",
            api_event_types=[planning_data.template.event_type.pk],
        ),
    )
    assert response.status_code == 302
    settings = PlanningSettings.objects.get()
    assert settings.api_enabled
    assert list(settings.api_event_types.all()) == [planning_data.template.event_type]
    assert client.get(url("api_now")).status_code == 200
