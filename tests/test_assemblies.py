"""Calling an assembly, inviting everybody who may see it and answering from the mail."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import AbstractParticipation, LocalParticipation, Shift
from ephios.core.models.users import Notification
from guardian.shortcuts import remove_perm

from ephios_shift_coordination.assemblies import (
    answer,
    answer_link,
    callable_types,
    can_call,
    invite,
    invited,
    own_state,
    plan_assembly,
    resolve_answer,
    would_invite,
)
from ephios_shift_coordination.models import Assembly
from ephios_shift_coordination.services import Conflict


def call(data, assembly_type, **changes):
    start = timezone.now() + timedelta(days=7)
    return plan_assembly(
        data.coordinator,
        **{
            "event_type": assembly_type,
            "title": "Monthly meeting",
            "description": "As every month",
            "location": "Back room",
            "start": start,
            "end": start + timedelta(hours=2),
            "agenda": "Welcome\nDuty plan",
            "silent": True,
            **changes,
        },
    )


@pytest.mark.django_db
def test_calling_an_assembly_creates_one_event_with_one_open_shift(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    event = assembly.event
    assert event.active and event.type == assembly_type
    shift = Shift.objects.get(event=event)
    # Everybody invited answers for themselves, and no qualification is required for that.
    assert shift.signup_flow_slug == "instant_confirmation"
    assert shift.structure_configuration["required_qualification_ids"] == []
    assert shift.signup_flow_configuration["user_can_decline_confirmed"] is True
    assert assembly.agenda.startswith("Welcome")
    assert set(invited(event)) == {
        planning_data.admin,
        planning_data.member,
        planning_data.coordinator,
    }


@pytest.mark.django_db
def test_the_recipients_are_known_before_anything_is_sent(planning_data, assembly_type):
    assert set(would_invite(planning_data.coordinator, assembly_type)) == {
        planning_data.admin,
        planning_data.member,
        planning_data.coordinator,
    }


@pytest.mark.django_db
def test_only_the_responsible_group_may_call_and_invite(planning_data, assembly_type):
    assert callable_types(planning_data.coordinator) == [assembly_type]
    assert callable_types(planning_data.member) == []
    start = timezone.now() + timedelta(days=1)
    with pytest.raises(PermissionDenied):
        plan_assembly(
            planning_data.member,
            event_type=assembly_type,
            title="Monthly meeting",
            description="",
            location="",
            start=start,
            end=start + timedelta(hours=1),
            agenda="",
            silent=True,
        )
    assembly = call(planning_data, assembly_type)
    with pytest.raises(PermissionDenied):
        invite(planning_data.member, assembly)


@pytest.mark.django_db
def test_an_ordinary_event_type_cannot_be_used_for_an_assembly(planning_data):
    assert callable_types(planning_data.coordinator) == []
    start = timezone.now() + timedelta(days=1)
    with pytest.raises(PermissionDenied):
        plan_assembly(
            planning_data.coordinator,
            event_type=planning_data.template.event_type,
            title="Monthly meeting",
            description="",
            location="",
            start=start,
            end=start + timedelta(hours=1),
            agenda="",
            silent=True,
        )


@pytest.mark.django_db
def test_an_assembly_has_to_end_after_it_starts(planning_data, assembly_type):
    start = timezone.now() + timedelta(days=1)
    with pytest.raises(ValidationError):
        call(planning_data, assembly_type, start=start, end=start)


@pytest.mark.django_db
def test_a_quiet_assembly_sends_nothing_until_the_invitation_is_sent(planning_data, assembly_type):
    before = Notification.objects.count()
    assembly = call(planning_data, assembly_type, silent=True)
    assert assembly.invited_at is None
    assert Notification.objects.count() == before
    recipients = invite(planning_data.coordinator, assembly)
    assembly.refresh_from_db()
    assert assembly.invited_at is not None
    assert set(recipients) == {
        planning_data.admin,
        planning_data.member,
        planning_data.coordinator,
    }
    assert Notification.objects.filter(slug="shift_coordination_assembly_invitation").count() == 3


@pytest.mark.django_db
def test_planning_out_loud_invites_right_away(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type, silent=False)
    assert assembly.invited_at is not None
    notification = Notification.objects.get(
        user=planning_data.member, slug="shift_coordination_assembly_invitation"
    )
    body = str(notification.body)
    assert "Welcome" in body and "Duty plan" in body and "Back room" in body
    # The link in the mail has to answer for this person, so it is followed, not compared:
    # every signed token carries its own timestamp and looks different each time.
    prefix = reverse("ephios_shift_coordination:assembly_respond", args=["token"])[: -len("token/")]
    token = body.split(prefix)[1].split("/")[0]
    assert resolve_answer(token) == (assembly, planning_data.member)
    assert "Monthly meeting" in str(notification.subject)


@pytest.mark.django_db
def test_the_signed_link_belongs_to_one_person_and_one_assembly(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    found, person = resolve_answer(answer_link(assembly, planning_data.member))
    assert found == assembly and person == planning_data.member
    with pytest.raises(PermissionDenied):
        resolve_answer(answer_link(assembly, planning_data.member) + "tampered")
    # A valid signature is not enough: whoever may not see the assembly cannot answer for it.
    with pytest.raises(PermissionDenied):
        resolve_answer(answer_link(assembly, planning_data.outsider))


@pytest.mark.django_db
def test_answering_writes_a_native_participation_and_can_be_changed(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    participation = answer(assembly, planning_data.member, attending=True)
    assert participation.state == AbstractParticipation.States.CONFIRMED
    assert answer(assembly, planning_data.member, attending=True).pk == participation.pk
    participation = answer(assembly, planning_data.member, attending=False)
    assert participation.state == AbstractParticipation.States.USER_DECLINED


@pytest.mark.django_db
def test_nobody_answers_an_assembly_that_is_over(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    Shift.objects.filter(event=assembly.event).update(
        start_time=timezone.now() - timedelta(hours=3),
        end_time=timezone.now() - timedelta(hours=1),
    )
    with pytest.raises(Conflict):
        answer(assembly, planning_data.member, attending=True)


@pytest.mark.django_db
def test_deleting_the_event_takes_the_assembly_with_it(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    assembly.event.delete()
    assert not Assembly.objects.filter(pk=assembly.pk).exists()


@pytest.mark.django_db
def test_an_ephios_administrator_may_call_any_assembly(planning_data, assembly_type):
    assert callable_types(planning_data.admin) == [assembly_type]


@pytest.mark.django_db
def test_nobody_calls_assemblies_while_the_plugin_is_off(planning_data, assembly_type):
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        name
        for name in preferences["general__enabled_plugins"]
        if name != "ephios_shift_coordination"
    ]
    assert not can_call(planning_data.coordinator, assembly_type)


@pytest.mark.django_db
def test_calling_needs_a_group_the_coordinator_may_publish_for(planning_data, assembly_type):
    remove_perm("publish_event_for_group", planning_data.coordination, planning_data.group)
    with pytest.raises(PermissionDenied):
        call(planning_data, assembly_type)


@pytest.mark.django_db
def test_an_impossible_event_is_reported_instead_of_saved(planning_data, assembly_type):
    with pytest.raises(ValidationError):
        call(planning_data, assembly_type, title="x" * 300)
    assert not Assembly.objects.exists()


@pytest.mark.django_db
def test_an_expired_link_asks_people_to_open_ephios(planning_data, assembly_type, monkeypatch):
    assembly = call(planning_data, assembly_type)
    token = answer_link(assembly, planning_data.member)
    monkeypatch.setattr("ephios_shift_coordination.assemblies.ANSWER_MAX_AGE", -1)
    with pytest.raises(Conflict, match="expired"):
        resolve_answer(token)


@pytest.mark.django_db
def test_a_rejected_participation_cannot_be_answered_around(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    answer(assembly, planning_data.member, attending=True)
    LocalParticipation.objects.update(state=AbstractParticipation.States.RESPONSIBLE_REJECTED)
    with pytest.raises(Conflict):
        answer(assembly, planning_data.member, attending=True)
    with pytest.raises(Conflict):
        answer(assembly, planning_data.member, attending=False)


@pytest.mark.django_db
def test_saying_no_twice_changes_nothing(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    declined = answer(assembly, planning_data.member, attending=False)
    assert answer(assembly, planning_data.member, attending=False).pk == declined.pk


@pytest.mark.django_db
def test_an_assembly_without_a_shift_is_no_longer_answerable(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    Shift.objects.filter(event=assembly.event).delete()
    assert own_state(assembly, planning_data.member) is None
    with pytest.raises(Conflict):
        answer(assembly, planning_data.member, attending=True)


@pytest.mark.django_db
def test_the_invitation_goes_out_once_the_transaction_commits(
    planning_data, assembly_type, monkeypatch, django_capture_on_commit_callbacks
):
    calls = []
    monkeypatch.setattr(
        "ephios.core.services.notifications.backends.send_all_notifications",
        lambda: calls.append("sent"),
    )
    with django_capture_on_commit_callbacks(execute=True):
        assembly = call(planning_data, assembly_type, silent=False)
        assert not calls  # nothing leaves the building before the assembly exists
    assert calls == ["sent"] and assembly.invited_at is not None
