"""Replacement overview and self-service staffing after publication."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from ephios.core.models import AbstractParticipation, LocalParticipation, UserProfile
from ephios.core.models.users import Notification

from ephios_shift_coordination.models import Availability, ObserverParticipation
from ephios_shift_coordination.publication import publish_plan
from ephios_shift_coordination.services import Conflict
from ephios_shift_coordination.staffing import (
    add_person,
    options,
    remove_person,
    replacement_access,
    replacements,
    staffing,
)
from ephios_shift_coordination.surveys import save_response
from tests.test_publication import prepare
from tests.test_surveys import answer, open_for
from tests.test_surveys import survey_data as survey_data


@pytest.fixture
def published_data(survey_data, monkeypatch):
    data = survey_data
    data.period = open_for(data)
    for person in (data.member, data.coordinator):
        response = data.period.responses.get(user=person)
        save_response(person, data.period.pk, **{**answer(response), "maximum": 2})
    monkeypatch.setattr("django.utils.timezone.now", lambda: data.period.deadline)
    publish_plan(
        data.coordinator, data.period.pk, **prepare(data, [[data.member.pk, data.shifts[0].pk]])
    )
    data.period.refresh_from_db()
    data.service = data.period.events.first()
    return data


def member_with_access(data, name, qualified=True):
    from ephios.core.models import QualificationGrant

    person = UserProfile.objects.create(
        email=f"{name}@example.invalid",
        display_name=name,
        date_of_birth=date(1990, 1, 1),
        is_active=True,
    )
    person.groups.add(data.group)
    if qualified:
        QualificationGrant.objects.create(
            user=person, qualification=data.template.shifts.first().qualifications.first()
        )
    return person


def test_replacement_overview_is_for_coordinators_and_the_people_staffed(published_data):
    data = published_data
    assert replacement_access(data.coordinator, data.service) is True
    assert replacement_access(data.member, data.service) is False
    for person in (data.outsider, member_with_access(data, "bystander")):
        with pytest.raises(PermissionDenied):
            replacement_access(person, data.service)
    data.period.state = data.period.State.PLANNING
    data.period.save()
    with pytest.raises(Conflict):
        replacement_access(data.coordinator, data.period.events.first())


def test_candidates_show_their_answer_the_possible_role_and_soft_warnings(published_data):
    data = published_data
    rows = replacements(data.service, coordinator=True)
    first = next(row for row in rows if row["planned"].pk == data.shifts[0].pk)
    candidates = {row["name"]: row for row in first["candidates"]}
    assert set(candidates) == {"coordinator"}  # the member is already staffed
    assert (
        candidates["coordinator"]["regular"] and candidates["coordinator"]["rating"] == "preferred"
    )
    assert not candidates["coordinator"]["warnings"] and not candidates["coordinator"]["blocked"]
    second = next(row for row in rows if row["planned"].pk == data.shifts[1].pk)
    assert not second["candidates"]  # nobody holds the qualification of that shift
    data.period.responses.filter(user=data.member).update(maximum=1)
    later = replacements(data.period.events.last(), coordinator=True)
    member = next(row for row in later[0]["candidates"] if row["name"] == "member")
    assert member["warnings"] and not member["blocked"]


def test_red_answer_excludes_an_offered_observer_role(published_data):
    data = published_data
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    response = data.period.responses.get(user=data.coordinator)
    response.availabilities.filter(planned_shift=data.shifts[0]).update(
        rating=Availability.Rating.UNAVAILABLE
    )
    response.observer_events.add(data.service)
    rows = replacements(data.service, coordinator=True)
    first = next(row for row in rows if row["planned"].pk == data.shifts[0].pk)
    assert not any(row["name"] == "coordinator" for row in first["candidates"])


def test_signing_off_warns_the_coordinators_about_a_gap(published_data):
    data = published_data
    planned = data.shifts[0]
    assert staffing(planned)["sufficient"]
    remove_person(data.member, planned.pk, user_id=data.member.pk)
    assert LocalParticipation.objects.get().state == AbstractParticipation.States.USER_DECLINED
    assert not staffing(planned)["sufficient"]
    warning = Notification.objects.get(slug="shift_coordination_understaffed")
    assert warning.user == data.coordinator and "member" in str(warning.body)
    assert not warning.is_obsolete and warning.get_actions()
    add_person(data.coordinator, planned.pk, user_id=data.coordinator.pk)
    assert staffing(planned)["sufficient"]
    warning.refresh_from_db()
    assert warning.is_obsolete


def test_signing_up_respects_capacity_qualifications_and_other_services(published_data):
    from tests.test_drafts import native_commitment

    data = published_data
    planned = data.shifts[0]
    add_person(data.coordinator, planned.pk, user_id=data.coordinator.pk)
    assert staffing(planned)["count"] == 2 and staffing(planned)["full"]
    with pytest.raises(ValidationError, match="fully staffed"):
        add_person(data.admin, planned.pk, user_id=data.admin.pk)
    with pytest.raises(Conflict, match="already staffed"):
        add_person(data.member, planned.pk, user_id=data.member.pk)
    with pytest.raises(ValidationError, match="qualifications"):
        add_person(data.coordinator, data.shifts[1].pk, user_id=data.coordinator.pk)
    with pytest.raises(ValidationError, match="Sitting in"):
        add_person(data.admin, data.shifts[1].pk, user_id=data.admin.pk, observer=True)
    remove_person(data.member, planned.pk, user_id=data.member.pk)
    native_commitment(data)
    with pytest.raises(ValidationError, match="another service"):
        add_person(data.member, planned.pk, user_id=data.member.pk)


def test_sitting_in_can_be_taken_and_given_back(published_data):
    data = published_data
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    planned = data.shifts[1]
    add_person(data.admin, planned.pk, user_id=data.admin.pk, observer=True)
    record = ObserverParticipation.objects.get()
    assert record.user == data.admin
    assert record.participation.state == AbstractParticipation.States.CONFIRMED
    assert [row["name"] for row in staffing(planned)["observers"]] == ["admin"]
    assert not staffing(planned)["sufficient"]  # a shift is never staffed by sitting in alone
    remove_person(data.admin, planned.pk, user_id=data.admin.pk)
    record.participation.refresh_from_db()
    assert record.participation.state == AbstractParticipation.States.USER_DECLINED
    add_person(data.admin, planned.pk, user_id=data.admin.pk, observer=True)
    assert ObserverParticipation.objects.count() == 1


def test_coordinators_staff_others_and_tell_them(published_data):
    data = published_data
    guest = member_with_access(data, "guest")
    planned = data.period.events.last().shifts.order_by("pk").first()
    add_person(data.coordinator, planned.pk, user_id=guest.pk)
    notice = Notification.objects.get(user=guest, slug="shift_coordination_staffing")
    assert "coordinator" in str(notice.body) and not notice.is_obsolete
    assert str(notice.subject)
    remove_person(data.coordinator, planned.pk, user_id=guest.pk)
    assert (
        LocalParticipation.objects.get(user=guest).state
        == AbstractParticipation.States.RESPONSIBLE_REJECTED
    )
    notice.refresh_from_db()
    assert notice.is_obsolete
    assert Notification.objects.filter(user=guest, slug="shift_coordination_staffing").count() == 2


def test_staffing_actions_are_protected(published_data, monkeypatch):
    data = published_data
    planned = data.shifts[0]
    with pytest.raises(PermissionDenied):
        add_person(data.member, planned.pk, user_id=data.coordinator.pk)
    with pytest.raises(ValidationError):
        add_person(data.coordinator, planned.pk, user_id=data.outsider.pk)
    with pytest.raises(ValidationError):
        add_person(data.coordinator, planned.pk, user_id=999999)
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: planned.shift.end_time + timedelta(days=1)
    )
    with pytest.raises(Conflict, match="already started"):
        remove_person(data.member, planned.pk, user_id=data.member.pk)


def test_replacement_views_check_roles_methods_and_csrf(client, published_data):
    data = published_data
    page_url = reverse("ephios_shift_coordination:replacement", args=[data.service.pk])
    action_url = reverse("ephios_shift_coordination:staffing_action", args=[data.shifts[0].pk])
    client.force_login(data.member)
    page = client.get(page_url)
    assert page.status_code == 200 and b"coordinator" in page.content
    assert b"private answer" not in page.content
    client.force_login(data.outsider)
    assert client.get(page_url).status_code == 403
    client.force_login(data.coordinator)
    assert client.get(page_url).status_code == 200
    assert client.get(action_url).status_code == 405
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(data.coordinator)
    assert (
        protected.post(action_url, {"user_id": data.member.pk, "action": "leave"}).status_code
        == 403
    )
    client.force_login(data.member)
    assert client.post(action_url, {"action": "wrong"}).status_code == 302
    assert LocalParticipation.objects.get().state == AbstractParticipation.States.CONFIRMED
    response = client.post(action_url, {"user_id": data.member.pk, "action": "leave"})
    assert response.status_code == 302 and response.url == page_url
    assert LocalParticipation.objects.get().state == AbstractParticipation.States.USER_DECLINED
    event_url = data.service.event.get_absolute_url()
    back = client.post(action_url, {"user_id": data.member.pk, "action": "join", "next": "event"})
    assert back.status_code == 302 and back.url == event_url
    assert LocalParticipation.objects.get().state == AbstractParticipation.States.CONFIRMED


def test_the_event_page_offers_self_service_after_publication(client, published_data):
    data = published_data
    client.force_login(data.member)
    page = client.get(data.service.event.get_absolute_url())
    assert b"I cannot make it" in page.content
    assert (
        reverse("ephios_shift_coordination:replacement", args=[data.service.pk]).encode()
        in page.content
    )
    client.force_login(data.coordinator)
    page = client.get(data.service.event.get_absolute_url())
    assert b"Take this shift" in page.content


def test_replacement_lists_people_who_offered_to_sit_in(published_data):
    data = published_data
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    data.period.responses.get(user=data.coordinator).observer_events.set([data.service.pk])
    rows = replacements(data.service, coordinator=True)
    second = next(row for row in rows if row["planned"].pk == data.shifts[1].pk)
    sitting = next(row for row in second["candidates"] if row["name"] == "coordinator")
    assert sitting["observer"] and not sitting["regular"] and not sitting["rating"]


def test_replacement_overview_without_answers_or_shifts_stays_empty(published_data):
    data = published_data
    data.period.responses.filter(user=data.coordinator).update(submitted_at=None)
    data.member.is_active = False
    data.member.save()
    assert all(not row["candidates"] for row in replacements(data.service, coordinator=True))
    data.service.shifts.all().delete()
    assert replacements(data.service, coordinator=True) == []


def test_staffing_needs_a_published_period_and_an_enabled_plugin(published_data):
    from dynamic_preferences.registries import global_preferences_registry

    data = published_data
    planned = data.shifts[0]
    with pytest.raises(Conflict, match="not staffed"):
        remove_person(data.coordinator, planned.pk, user_id=data.coordinator.pk)
    data.period.state = data.period.State.PLANNING
    data.period.save()
    with pytest.raises(Conflict, match="not been published"):
        add_person(data.coordinator, planned.pk, user_id=data.coordinator.pk)
    data.period.state = data.period.State.PUBLISHED
    data.period.save()
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        name
        for name in preferences["general__enabled_plugins"]
        if name != "ephios_shift_coordination"
    ]
    with pytest.raises(PermissionDenied):
        add_person(data.coordinator, planned.pk, user_id=data.coordinator.pk)


def test_failed_self_service_keeps_the_page_and_explains_why(client, published_data):
    data = published_data
    action_url = reverse("ephios_shift_coordination:staffing_action", args=[data.shifts[1].pk])
    client.force_login(data.member)
    response = client.post(action_url, {"user_id": data.member.pk, "action": "join"}, follow=True)
    assert response.status_code == 200
    assert b"qualifications" in response.content
    assert not LocalParticipation.objects.filter(shift=data.shifts[1].shift).exists()


def simultaneous(data):
    from ephios.core.models import Shift

    first, second = data.shifts
    Shift.objects.filter(pk=second.shift_id).update(
        start_time=first.shift.start_time, end_time=first.shift.end_time
    )
    second.shift.refresh_from_db()
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    for planned in (first, second):
        planned.event.period = data.period
    return first, second


def test_concurrent_self_service_allows_observer_but_not_second_regular_role(published_data):
    from ephios.core.models import QualificationGrant

    data = published_data
    phone, chat = simultaneous(data)
    QualificationGrant.objects.create(
        user=data.member, qualification=data.template.shifts.last().qualifications.first()
    )
    state = options(data.member, chat)
    assert state["can_observe"] and not state["can_join"]
    add_person(data.member, chat.pk, user_id=data.member.pk, observer=True)
    assert options(data.member, chat)["can_leave"]
    remove_person(data.member, chat.pk, user_id=data.member.pk)
    with pytest.raises(ValidationError, match="another service"):
        add_person(data.member, chat.pk, user_id=data.member.pk)
    # Move the regular duty to chat, then sit in on the phone shift.
    remove_person(data.member, phone.pk, user_id=data.member.pk)
    add_person(data.member, phone.pk, user_id=data.member.pk, observer=True)
    assert options(data.member, chat)["can_join"]
    add_person(data.member, chat.pk, user_id=data.member.pk)
    assert staffing(phone)["observers"][0]["user"] == data.member
    assert staffing(chat)["regular"][0]["user"] == data.member


def test_replacement_list_offers_concurrent_beisitz_without_separate_offer(published_data):
    data = published_data
    _phone, chat = simultaneous(data)
    rows = replacements(data.service, coordinator=True)
    row = next(row for row in rows if row["planned"].pk == chat.pk)
    candidate = next(
        candidate for candidate in row["candidates"] if candidate["user"] == data.member
    )
    assert candidate["observer"] and not candidate["regular"] and not candidate["blocked"]


@pytest.mark.parametrize("other_type", [False, True])
def test_existing_observer_conflicts_respect_event_type_and_effective_times(
    published_data, other_type
):
    from tests.test_drafts import native_observer

    data = published_data
    _phone, chat = simultaneous(data)
    existing = native_observer(data, other_type=other_type)
    assert options(data.member, chat)["can_observe"] is other_type
    if not other_type:
        with pytest.raises(ValidationError, match="another service"):
            add_person(data.member, chat.pk, user_id=data.member.pk, observer=True)
    existing.individual_start_time = chat.shift.end_time
    existing.individual_end_time = chat.shift.end_time + timedelta(hours=1)
    existing.save()
    assert options(data.member, chat)["can_observe"]
    add_person(data.member, chat.pk, user_id=data.member.pk, observer=True)


def test_observer_only_self_service_can_cover_multiple_simultaneous_sibling_shifts(published_data):
    data = published_data
    phone, chat = simultaneous(data)
    remove_person(data.member, phone.pk, user_id=data.member.pk)
    for planned in (phone, chat):
        assert options(data.member, planned)["can_observe"]
        add_person(data.member, planned.pk, user_id=data.member.pk, observer=True)
    assert ObserverParticipation.objects.filter(user=data.member).count() == 2
