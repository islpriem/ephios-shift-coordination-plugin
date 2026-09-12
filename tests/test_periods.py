import uuid
from datetime import UTC, date, datetime, time
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from ephios.core.models import Event, LocalParticipation, Shift, UserProfile
from guardian.shortcuts import remove_perm

from ephios_shift_coordination.models import PlannedEvent, PlanningPeriod
from ephios_shift_coordination.services import Conflict, create_period


def request_data(data):
    return dict(
        template_id=data.template.pk,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        dates=[date(2026, 10, 3), date(2026, 10, 6)],
        rules=data.configuration.snapshot(),
        creation_key=uuid.uuid4(),
    )


def test_series_creates_native_visible_events_without_signups(planning_data):
    data = planning_data
    period = create_period(data.coordinator, **request_data(data))
    assert period.state == PlanningPeriod.State.PREPARATION
    assert Event.objects.count() == 2
    assert Shift.objects.count() == 4
    assert not LocalParticipation.objects.exists()
    for event in Event.objects.all():
        assert data.member.has_perm("core.view_event", event)
        assert data.coordinator.has_perm("core.change_event", event)
        assert not data.outsider.has_perm("core.view_event", event)
        for shift in event.shifts.all():
            assert shift.signup_flow.slug == "manual"
            assert shift.structure.slug == "uniform"
            assert not shift.signup_flow.get_validator(data.member.as_participant()).can_sign_up()
            assert shift.structure_configuration["required_qualification_ids"]
    data.configuration.weekly_limit = 8
    data.configuration.save()
    data.template.title = "Changed template"
    data.template.save()
    period.refresh_from_db()
    assert period.rules["weekly_limit"] == 2
    assert period.template_snapshot["title"] == "Duty"
    assert period.events.first().date == date(2026, 10, 3)


def test_creation_replay_is_idempotent_but_changed_payload_is_a_conflict(planning_data):
    arguments = request_data(planning_data)
    first = create_period(planning_data.coordinator, **arguments)
    assert create_period(planning_data.coordinator, **arguments).pk == first.pk
    with pytest.raises(Conflict):
        create_period(planning_data.coordinator, **{**arguments, "dates": [date(2026, 10, 4)]})
    with pytest.raises(PermissionDenied):
        create_period(planning_data.admin, **arguments)
    assert Event.objects.count() == 2


def test_creation_rolls_back_all_native_objects_on_failure(planning_data):
    original = Shift.save

    def fail_second_shift(shift, *args, **kwargs):
        if Shift.objects.exists():
            raise RuntimeError("Injected database failure")
        return original(shift, *args, **kwargs)

    with patch.object(Shift, "save", fail_second_shift), pytest.raises(RuntimeError):
        create_period(planning_data.coordinator, **request_data(planning_data))
    assert not Event.all_objects.exists()
    assert not PlanningPeriod.objects.exists()
    assert not PlannedEvent.objects.exists()


@pytest.mark.parametrize(
    "permission",
    ["core.add_event", "publish_event_for_group", "ephios_shift_coordination.manage_planning"],
)
def test_creation_rechecks_native_and_plugin_permissions(planning_data, permission):
    data = planning_data
    if permission == "publish_event_for_group":
        remove_perm(permission, data.coordination, data.group)
    else:
        remove_perm(permission, data.coordination)
    user = UserProfile.objects.get(pk=data.coordinator.pk)
    with pytest.raises(PermissionDenied):
        create_period(user, **request_data(data))
    assert not Event.all_objects.exists()


@pytest.mark.parametrize("days", [[], [date(2026, 9, 30)], [date(2026, 10, 3)] * 2])
def test_invalid_date_selection_is_rejected(planning_data, days):
    with pytest.raises(ValidationError):
        create_period(planning_data.coordinator, **{**request_data(planning_data), "dates": days})
    assert not Event.all_objects.exists()


def test_overnight_service_and_dst_conflicts(planning_data):
    shift = planning_data.template.shifts.first()
    shift.meeting_time, shift.start_time, shift.end_time = time(21, 45), time(22), time(6)
    shift.end_day_offset = 1
    shift.save()
    period = create_period(planning_data.coordinator, **request_data(planning_data))
    native = period.events.first().shifts.first().shift
    assert native.end_time.date() > native.start_time.date()
    shift.start_time = time(2, 30)
    shift.meeting_time = time(2)
    shift.save()
    with pytest.raises(ValidationError, match="ambiguous"):
        create_period(
            planning_data.coordinator,
            **{**request_data(planning_data), "dates": [date(2026, 10, 25)]},
        )
    assert PlanningPeriod.objects.count() == 1


def test_template_without_shifts_and_missing_native_plugins_cannot_create_events(planning_data):
    from dynamic_preferences.registries import global_preferences_registry

    data = planning_data
    preferences = global_preferences_registry.manager()
    original = preferences["general__enabled_plugins"]
    preferences["general__enabled_plugins"] = [
        p for p in original if p != "ephios.plugins.basesignupflows"
    ]
    with pytest.raises(ValidationError, match="Enable"):
        create_period(data.coordinator, **request_data(data))
    preferences["general__enabled_plugins"] = original
    data.template.shifts.all().delete()
    with pytest.raises(ValidationError, match="at least one shift"):
        create_period(data.coordinator, **request_data(data))
    assert not Event.all_objects.exists()


def test_period_can_disable_reminders(planning_data):
    data = request_data(planning_data)
    data["rules"]["reminder_days"] = []
    assert create_period(planning_data.coordinator, **data).rules["reminder_days"] == []


def test_series_rejects_a_shift_that_has_already_started(planning_data):
    with (
        patch("django.utils.timezone.now", return_value=datetime(2026, 10, 3, 12, tzinfo=UTC)),
        pytest.raises(ValidationError, match="future"),
    ):
        create_period(planning_data.coordinator, **request_data(planning_data))
    assert not Event.all_objects.exists()
