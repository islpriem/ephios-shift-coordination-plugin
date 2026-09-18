"""Reminders before a service or an assembly, observed through a controlled clock."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import AbstractParticipation, Event, LocalParticipation, Shift
from ephios.core.models.users import Notification

from ephios_shift_coordination.models import Assembly, PlanningSettings, ReminderDispatch
from ephios_shift_coordination.notifications import AssemblyReminder
from ephios_shift_coordination.reminders import process_reminders

from .test_assemblies import call
from .test_observers import observer_data as observer_data
from .test_surveys import survey_data as survey_data


@pytest.fixture
def clock(monkeypatch):
    """Move the clock so a periodic run can be watched at a chosen moment."""

    def move(moment):
        monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
        return moment

    return move


def service(planning_data, *, start, people=(), observers=()):
    """A plain service event, as an event type marked 'service' has outside the plugin."""
    event_type = planning_data.template.event_type
    event_type.preferences["shift_coordination__is_service"] = True
    event = Event.objects.create(
        title="Duty", location="Test location", type=event_type, active=True
    )
    shift = Shift.objects.create(
        event=event,
        meeting_time=start,
        start_time=start,
        end_time=start + timedelta(hours=4),
        signup_flow_slug="manual",
        signup_flow_configuration={"no_selfservice_explanation": ""},
        structure_slug="uniform",
        structure_configuration={
            "required_qualification_ids": [],
            "minimum_number_of_participants": 1,
            "maximum_number_of_participants": None,
        },
    )
    for person in people:
        LocalParticipation.objects.create(
            shift=shift, user=person, state=AbstractParticipation.States.CONFIRMED
        )
    return shift


def rules(*, service_days=(), assembly_days=(), at="09:00"):
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    settings.service_reminder_days = list(service_days)
    settings.assembly_reminder_days = list(assembly_days)
    settings.service_reminder_time = at
    settings.assembly_reminder_time = at
    settings.save()


def reminders(user=None, slug=None):
    found = Notification.objects.filter(slug__contains="_reminder")
    if user:
        found = found.filter(user=user)
    if slug:
        found = found.filter(slug=slug)
    return list(found)


@pytest.mark.django_db
def test_one_reminder_per_person_and_appointment(planning_data, clock):
    rules(service_days=[1])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    service(planning_data, start=start, people=[planning_data.member])
    clock(datetime(2026, 9, 8, 12, tzinfo=UTC))  # still too early
    process_reminders()
    assert not reminders()
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))  # 09:00 Berlin on the day before
    process_reminders()
    assert len(reminders(planning_data.member)) == 1
    process_reminders()  # a second periodic run changes nothing
    assert len(reminders(planning_data.member)) == 1


@pytest.mark.django_db
def test_a_run_after_downtime_sends_only_the_newest_reminder(planning_data, clock):
    rules(service_days=[3, 1])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    service(planning_data, start=start, people=[planning_data.member])
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    process_reminders()
    assert len(reminders(planning_data.member)) == 1
    assert ReminderDispatch.objects.filter(skipped=True).count() == 1


@pytest.mark.django_db
def test_without_rules_nothing_is_reminded(planning_data, clock):
    rules()
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    service(planning_data, start=start, people=[planning_data.member])
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    process_reminders()
    assert not reminders() and not ReminderDispatch.objects.exists()


@pytest.mark.django_db
def test_an_unmarked_event_type_is_never_reminded(planning_data, clock):
    rules(service_days=[1])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    shift = service(planning_data, start=start, people=[planning_data.member])
    shift.event.type.preferences["shift_coordination__is_service"] = False
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    process_reminders()
    assert not reminders()


@pytest.mark.django_db
def test_an_assembly_reminds_everybody_invited_whatever_they_answered(
    planning_data, assembly_type, clock
):
    from ephios_shift_coordination.assemblies import answer

    rules(assembly_days=[1])
    assembly = call(planning_data, assembly_type)
    answer(assembly, planning_data.member, attending=False)
    shift = assembly.event.shifts.first()
    # Midday on the day before, so the rule's 09:00 has passed and the assembly has not.
    before = (timezone.localtime(shift.start_time) - timedelta(days=1)).replace(hour=12)
    clock(before)
    process_reminders()
    assert len(reminders(planning_data.member, "shift_coordination_assembly_reminder")) == 1


@pytest.mark.django_db
def test_somebody_sitting_in_is_reminded_like_everybody_else(observer_data, clock):
    from ephios_shift_coordination.publication import publish_plan

    from .test_publication import prepare

    data = observer_data
    shift = data.shifts[0]
    publish_plan(
        data.coordinator,
        data.period.pk,
        **prepare(data, [[data.member.pk, shift.pk]], observers=[[data.admin.pk, shift.pk]]),
    )
    data.period.template.event_type.preferences["shift_coordination__is_service"] = True
    rules(service_days=[1])
    native = shift.shift
    clock((timezone.localtime(native.start_time) - timedelta(days=1)).replace(hour=12))
    process_reminders()
    assert len(reminders(data.member, "shift_coordination_service_reminder")) == 1
    assert len(reminders(data.admin, "shift_coordination_service_reminder")) == 1
    sitting = Notification.objects.get(user=data.admin, slug="shift_coordination_service_reminder")
    assert "sitting in" in str(sitting.body) and "member" in str(sitting.body)
    staffed = Notification.objects.get(user=data.member, slug="shift_coordination_service_reminder")
    assert "sitting in" not in str(staffed.body) and "Duty" in str(staffed.subject)
    assert not staffed.get_actions() and not staffed.is_obsolete
    clock(native.start_time + timedelta(minutes=1))
    assert staffed.is_obsolete


@pytest.mark.django_db
def test_a_reminder_for_a_shift_that_disappeared_stays_readable(planning_data, clock):
    rules(service_days=[1])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    shift = service(planning_data, start=start, people=[planning_data.member])
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    process_reminders()
    notification = Notification.objects.get(user=planning_data.member)
    shift.delete()
    assert str(notification.body) == "" and notification.is_obsolete
    assert str(notification.subject)


@pytest.mark.django_db
def test_the_responsible_sees_which_reminders_went_out_and_which_comes_next(
    planning_data, assembly_type, clock
):
    from ephios_shift_coordination.assemblies import shift_of
    from ephios_shift_coordination.reminders import overview

    rules(assembly_days=[3, 1])
    assembly = call(planning_data, assembly_type)
    shift = shift_of(assembly)
    assert overview(shift)["sent"] == [] and overview(shift)["next"] is not None
    clock((timezone.localtime(shift.start_time) - timedelta(days=1)).replace(hour=12))
    process_reminders()
    state = overview(shift)
    assert len(state["sent"]) == 1 and state["next"] is None


@pytest.mark.django_db
def test_a_service_without_partners_or_a_place_still_reads_well(planning_data, clock):
    rules(service_days=[0])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    shift = service(planning_data, start=start, people=[planning_data.member])
    Event.objects.filter(pk=shift.event_id).update(location="")
    clock(datetime(2026, 9, 10, 8, tzinfo=UTC))
    process_reminders()
    body = str(Notification.objects.get(user=planning_data.member).body)
    assert "Where:" not in body and "With you" not in body
    assert "you are staffed for Duty" in body


@pytest.mark.django_db
def test_nothing_is_reminded_while_the_plugin_is_off(planning_data, clock):
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        name
        for name in preferences["general__enabled_plugins"]
        if name != "ephios_shift_coordination"
    ]
    rules(service_days=[1])
    start = datetime(2026, 9, 10, 9, tzinfo=UTC)
    service(planning_data, start=start, people=[planning_data.member])
    clock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    process_reminders()
    assert not reminders()


@pytest.mark.django_db
def test_an_assembly_reminder_reads_like_the_invitation(planning_data, assembly_type, clock):
    rules(assembly_days=[1])
    assembly = call(planning_data, assembly_type)
    shift = assembly.event.shifts.first()
    clock((timezone.localtime(shift.start_time) - timedelta(days=1)).replace(hour=12))
    process_reminders()
    notification = Notification.objects.get(
        user=planning_data.member, slug="shift_coordination_assembly_reminder"
    )
    assert str(notification.subject).startswith("Reminder: Monthly meeting")
    assert "Welcome" in str(notification.body) and "answer here" in str(notification.body)
    Assembly.objects.all().delete()
    assert str(notification.subject) == str(AssemblyReminder.title)
