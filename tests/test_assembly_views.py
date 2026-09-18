"""The assembly pages: the list, calling one, sending the invitation and answering."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import AbstractParticipation, LocalParticipation
from ephios.core.models.users import Notification

from ephios_shift_coordination.assemblies import answer_link, shift_of
from ephios_shift_coordination.models import Assembly
from ephios_shift_coordination.signals import event_info

from .test_assemblies import call

NAMESPACE = "ephios_shift_coordination:"


def url(name, *args):
    return reverse(NAMESPACE + name, args=args)


def form_data(**changes):
    day = (timezone.now() + timedelta(days=10)).date()
    return {
        "title": "Monthly meeting",
        "location": "Back room",
        "description": "As every month",
        "date": day.isoformat(),
        "start": "19:00",
        "end": "21:00",
        "agenda": "Welcome\nDuty plan",
        **changes,
    }


@pytest.mark.parametrize("role, status", [("coordinator", 200), ("member", 200), ("outsider", 200)])
def test_everybody_sees_the_assemblies_they_are_invited_to(planning_data, client, role, status):
    client.force_login(getattr(planning_data, role))
    assert client.get(url("assembly_list")).status_code == status


@pytest.mark.django_db
def test_the_list_shows_assemblies_only_to_the_people_invited(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.member)
    assert assembly.event.title in client.get(url("assembly_list")).content.decode()
    client.force_login(planning_data.outsider)
    assert assembly.event.title not in client.get(url("assembly_list")).content.decode()


@pytest.mark.django_db
def test_only_a_responsible_reaches_the_form(planning_data, client, assembly_type):
    client.force_login(planning_data.member)
    assert client.get(url("assembly_create")).status_code == 403
    client.force_login(planning_data.coordinator)
    page = client.get(url("assembly_create"))
    assert page.status_code == 200
    # The recipients are visible before anything is sent.
    assert "member" in page.content.decode()


@pytest.mark.django_db
def test_calling_quietly_sends_nothing_and_the_button_sends_later(
    planning_data, client, assembly_type
):
    client.force_login(planning_data.coordinator)
    response = client.post(
        url("assembly_create"),
        form_data(event_type=assembly_type.pk, silent="on"),
    )
    assembly = Assembly.objects.get()
    assert response.status_code == 302 and response.url == assembly.event.get_absolute_url()
    assert assembly.invited_at is None and not Notification.objects.exists()
    assert client.post(url("assembly_invite", assembly.pk)).status_code == 302
    assembly.refresh_from_db()
    assert assembly.invited_at is not None and Notification.objects.count() == 3


@pytest.mark.django_db
def test_somebody_else_cannot_send_the_invitation(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.member)
    assert client.post(url("assembly_invite", assembly.pk)).status_code == 403


@pytest.mark.django_db
def test_an_assembly_in_the_past_is_refused_with_a_readable_message(
    planning_data, client, assembly_type
):
    client.force_login(planning_data.coordinator)
    yesterday = (timezone.now() - timedelta(days=1)).date()
    page = client.post(
        url("assembly_create"), form_data(event_type=assembly_type.pk, date=yesterday.isoformat())
    )
    assert page.status_code == 200 and not Assembly.objects.exists()
    assert "has to start in the future" in page.content.decode()


@pytest.mark.django_db
def test_the_link_from_the_mail_answers_without_a_login(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    address = url("assembly_respond", answer_link(assembly, planning_data.member))
    page = client.get(address)
    assert page.status_code == 200 and "not answered yet" in page.content.decode()
    # Nothing is written on GET, so mail scanners cannot answer for anybody.
    assert not LocalParticipation.objects.exists()
    assert client.post(address, {"action": "yes"}).status_code == 200
    participation = LocalParticipation.objects.get(user=planning_data.member)
    assert participation.state == AbstractParticipation.States.CONFIRMED
    client.post(address, {"action": "no"})
    participation.refresh_from_db()
    assert participation.state == AbstractParticipation.States.USER_DECLINED


@pytest.mark.django_db
def test_a_broken_link_is_refused_and_an_ended_assembly_says_so(
    planning_data, client, assembly_type
):
    assembly = call(planning_data, assembly_type)
    assert client.get(url("assembly_respond", "not-a-token")).status_code == 403
    address = url("assembly_respond", answer_link(assembly, planning_data.member))
    shift = shift_of(assembly)
    shift.start_time = timezone.now() - timedelta(hours=3)
    shift.end_time = timezone.now() - timedelta(hours=1)
    shift.save()
    page = client.post(address, {"action": "yes"})
    assert page.status_code == 409 and "is over" in page.content.decode()


@pytest.mark.django_db
def test_the_event_page_shows_the_agenda_and_the_invitation_state(
    planning_data, client, assembly_type
):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.coordinator)
    page = client.get(assembly.event.get_absolute_url()).content.decode()
    assert "Duty plan" in page and "Nobody has been invited yet" in page
    assert "Send the invitation now" in page
    client.force_login(planning_data.member)
    page = client.get(assembly.event.get_absolute_url()).content.decode()
    assert "Duty plan" in page and "Send the invitation now" not in page


@pytest.mark.django_db
def test_an_ordinary_event_carries_no_assembly_panel(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    event = assembly.event
    Assembly.objects.filter(pk=assembly.pk).delete()
    client.force_login(planning_data.coordinator)
    assert "Agenda" not in client.get(event.get_absolute_url()).content.decode()


@pytest.mark.django_db
def test_the_form_starts_from_the_first_kind_of_assembly(planning_data, client, assembly_type):
    client.force_login(planning_data.coordinator)
    page = client.get(url("assembly_create")).content.decode()
    assert 'value="Monthly meeting"' in page and 'value="Back room"' in page
    assert "Default: Monthly meeting in Back room" in page


@pytest.mark.django_db
def test_an_incomplete_form_comes_back_with_its_errors(planning_data, client, assembly_type):
    client.force_login(planning_data.coordinator)
    data = form_data(event_type=assembly_type.pk)
    del data["date"]
    page = client.post(url("assembly_create"), data)
    assert page.status_code == 200 and not Assembly.objects.exists()
    page = client.post(
        url("assembly_create"), form_data(event_type=assembly_type.pk, start="21:00", end="19:00")
    )
    assert "has to end after it starts" in page.content.decode()


@pytest.mark.django_db
def test_an_event_ephios_refuses_is_shown_on_the_form(
    planning_data, client, assembly_type, monkeypatch
):
    def refuse(*args, **kwargs):
        raise ValidationError("ephios refused this event")

    monkeypatch.setattr("ephios_shift_coordination.views.plan_assembly", refuse)
    client.force_login(planning_data.coordinator)
    page = client.post(url("assembly_create"), form_data(event_type=assembly_type.pk))
    assert page.status_code == 200 and "ephios refused this event" in page.content.decode()


@pytest.mark.django_db
def test_the_answer_link_stops_working_when_the_plugin_is_switched_off(
    planning_data, client, assembly_type
):
    assembly = call(planning_data, assembly_type)
    address = url("assembly_respond", answer_link(assembly, planning_data.member))
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        name
        for name in preferences["general__enabled_plugins"]
        if name != "ephios_shift_coordination"
    ]
    assert client.get(address).status_code == 404


@pytest.mark.django_db
def test_an_expired_link_explains_itself(planning_data, client, assembly_type, monkeypatch):
    assembly = call(planning_data, assembly_type)
    address = url("assembly_respond", answer_link(assembly, planning_data.member))
    monkeypatch.setattr("ephios_shift_coordination.assemblies.ANSWER_MAX_AGE", -1)
    page = client.get(address)
    assert page.status_code == 409 and "expired" in page.content.decode()


@pytest.mark.django_db
def test_the_panel_never_reaches_somebody_who_may_not_see_the_event(
    planning_data, assembly_type, rf
):
    assembly = call(planning_data, assembly_type)
    request = rf.get(assembly.event.get_absolute_url())
    request.user = planning_data.outsider
    assert event_info(None, request=request, event=assembly.event) == ""
