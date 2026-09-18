import uuid
from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.test import Client, RequestFactory
from django.urls import reverse
from dynamic_preferences.registries import global_preferences_registry

from ephios_shift_coordination.models import PlanningPeriod, PlanningSettings

NAMESPACE = "ephios_shift_coordination:"


def url(name, *args):
    return reverse(NAMESPACE + name, args=args)


def settings_payload(configuration, **changes):
    """The whole settings form: planning rules plus the instance-wide reminder rules."""
    return {
        **configuration.snapshot(),
        "service_reminder_time": "09:00",
        "assembly_reminder_time": "09:00",
        "next_period_weeks": 2,
        **changes,
    }


@pytest.mark.parametrize(
    "role, page, status",
    [
        ("admin", "settings", 200),
        ("coordinator", "settings", 403),
        ("member", "settings", 403),
        ("admin", "template_list", 200),
        ("coordinator", "template_list", 403),
        ("coordinator", "period_list", 200),
        ("member", "period_list", 403),
        ("member", "survey_list", 200),
        ("outsider", "survey_list", 200),
    ],
)
def test_role_matrix(planning_data, client, role, page, status):
    client.force_login(getattr(planning_data, role))
    assert client.get(url(page)).status_code == status


def test_anonymous_and_disabled_plugin(planning_data, client):
    assert client.get(url("period_list")).status_code == 302
    client.force_login(planning_data.admin)
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        p for p in preferences["general__enabled_plugins"] if p != "ephios_shift_coordination"
    ]
    assert client.get(url("settings")).status_code == 404


def test_csrf_protects_settings(planning_data):
    client = Client(enforce_csrf_checks=True)
    client.force_login(planning_data.admin)
    assert client.post(url("settings"), {}).status_code == 403


def test_administrator_selects_existing_groups_and_defaults(planning_data, client):
    client.force_login(planning_data.admin)
    group = Group.objects.create(name="New planning group")
    payload = settings_payload(
        planning_data.configuration,
        planning_groups=[group.pk],
        weekdays=[1, 3],
        reminder_days="3, 1",
        weekly_limit=4,
    )
    response = client.post(url("settings"), payload)
    assert response.status_code == 302
    assert group.permissions.filter(codename="manage_planning").exists()
    assert not planning_data.coordination.permissions.filter(codename="manage_planning").exists()
    assert PlanningSettings.objects.get().weekly_limit == 4
    client.force_login(planning_data.coordinator)
    assert client.get(url("period_list")).status_code == 403


def test_calendar_preview_preserves_manual_selection_and_create_is_explicit(planning_data, client):
    client.force_login(planning_data.coordinator)
    payload = {
        **planning_data.configuration.snapshot(),
        "template": planning_data.template.pk,
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "creation_key": str(uuid.uuid4()),
        "reminder_days": "3",
    }
    initial = client.post(url("period_create"), {**payload, "action": "calendar"})
    assert initial.status_code == 200
    assert not PlanningPeriod.objects.exists()
    manual = {**payload, "selection_ready": "1", "dates": ["2026-10-03"], "action": "preview"}
    preview = client.post(url("period_create"), manual)
    assert preview.status_code == 200
    assert preview.context["selected_dates"] == [date(2026, 10, 3)]
    assert not PlanningPeriod.objects.exists()
    created = client.post(url("period_create"), {**manual, "action": "create"})
    assert created.status_code == 302
    period = PlanningPeriod.objects.get()
    assert period.events.count() == 1
    assert client.get(created.url).status_code == 200
    assert client.post(url("period_create"), {**manual, "action": "create"}).url == created.url


def template_payload(template):
    payload = {
        "title": template.title,
        "location": "Updated location",
        "description": "<script>alert('escaped')</script>",
        "event_type": template.event_type_id,
        "visible_for": list(template.visible_for.values_list("pk", flat=True)),
        "responsible_groups": list(template.responsible_groups.values_list("pk", flat=True)),
        "shifts-TOTAL_FORMS": 2,
        "shifts-INITIAL_FORMS": 2,
        "shifts-MIN_NUM_FORMS": 1,
        "shifts-MAX_NUM_FORMS": 1000,
    }
    for index, shift in enumerate(template.shifts.all()):
        for field in (
            "id",
            "label",
            "meeting_time",
            "start_time",
            "end_time",
            "end_day_offset",
            "minimum",
            "maximum",
        ):
            payload[f"shifts-{index}-{field}"] = getattr(shift, field)
        payload[f"shifts-{index}-qualifications"] = list(
            shift.qualifications.values_list("pk", flat=True)
        )
    return payload


def test_admin_edits_template_and_invalid_shift_does_not_partially_save(planning_data, client):
    client.force_login(planning_data.admin)
    edit_url = url("template_edit", planning_data.template.pk)
    assert client.get(url("template_create")).status_code == 200
    assert client.get(edit_url).status_code == 200
    payload = template_payload(planning_data.template)
    invalid = client.post(edit_url, {**payload, "shifts-0-minimum": 0})
    assert invalid.status_code == 200
    planning_data.template.refresh_from_db()
    assert planning_data.template.location == "Test location"
    assert client.post(edit_url, payload).status_code == 302
    result = client.get(edit_url)
    assert b"&lt;script&gt;" in result.content
    assert b"<script>alert" not in result.content
    planning_data.template.refresh_from_db()
    assert planning_data.template.location == "Updated location"


def test_added_shift_stays_after_existing_template_shifts(planning_data, client):
    client.force_login(planning_data.admin)
    payload = template_payload(planning_data.template)
    payload["shifts-TOTAL_FORMS"] = 3
    payload.update(
        {
            key.replace("shifts-0-", "shifts-2-"): value
            for key, value in payload.copy().items()
            if key.startswith("shifts-0-") and key != "shifts-0-id"
        }
    )
    payload["shifts-2-label"] = "Added shift"
    assert client.post(url("template_edit", planning_data.template.pk), payload).status_code == 302
    assert list(planning_data.template.shifts.values_list("label", flat=True)) == [
        "Shift 1",
        "Shift 2",
        "Added shift",
    ]


def test_native_group_form_cannot_grant_planning_outside_admin_settings(planning_data):
    from ephios.core.forms.users import GroupForm

    form = GroupForm(
        instance=planning_data.group,
        data={"name": planning_data.group.name, "manage_planning": "on"},
    )
    assert form.is_valid(), form.errors
    form.save()
    assert not planning_data.group.permissions.filter(codename="manage_planning").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "2026-12-31", "end_date": "2026-10-31"},
        {"dates": ["not-a-date"]},
        {"dates": []},
        {"reminder_days": "invalid"},
        {"weekdays": []},
        {"action": "create", "selection_ready": ""},
    ],
)
def test_invalid_period_submission_creates_nothing(planning_data, client, changes):
    client.force_login(planning_data.coordinator)
    payload = {
        **planning_data.configuration.snapshot(),
        "template": planning_data.template.pk,
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "creation_key": str(uuid.uuid4()),
        "reminder_days": "3",
        "action": "create",
        "selection_ready": "1",
        "dates": ["2026-10-03"],
    }
    response = client.post(url("period_create"), {**payload, **changes})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not PlanningPeriod.objects.exists()


def test_replayed_creation_with_changed_selection_returns_409(planning_data, client):
    client.force_login(planning_data.coordinator)
    payload = {
        **planning_data.configuration.snapshot(),
        "template": planning_data.template.pk,
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "creation_key": str(uuid.uuid4()),
        "reminder_days": "3",
        "action": "create",
        "selection_ready": "1",
        "dates": ["2026-10-03"],
    }
    assert client.post(url("period_create"), payload).status_code == 302
    assert (
        client.post(url("period_create"), {**payload, "dates": ["2026-10-04"]}).status_code == 409
    )
    assert PlanningPeriod.objects.count() == 1


def test_empty_reminders_can_be_saved(planning_data, client):
    client.force_login(planning_data.admin)
    response = client.post(
        url("settings"), settings_payload(planning_data.configuration, reminder_days="")
    )
    assert response.status_code == 302
    assert PlanningSettings.objects.get().reminder_days == []


def test_holiday_and_solver_values_come_from_the_planning_settings(planning_data, client):
    data = planning_data
    data.configuration.region = ""
    data.configuration.save()
    client.force_login(data.coordinator)
    page = client.get(url("period_create"))
    assert page.context["form"].fields["exclude_holidays"].disabled
    assert "solver_seconds" not in page.context["form"].fields
    payload = {
        **data.configuration.snapshot(),
        "template": data.template.pk,
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "creation_key": str(uuid.uuid4()),
        "reminder_days": "3",
        "action": "create",
        "selection_ready": "1",
        "dates": ["2026-10-06"],
        "exclude_holidays": True,
        "solver_seconds": 99,
        "region": "BE",
    }
    assert client.post(url("period_create"), payload).status_code == 302
    rules = PlanningPeriod.objects.get().rules
    assert rules["exclude_holidays"] is False and rules["region"] == ""
    assert rules["solver_seconds"] == data.configuration.solver_seconds


def test_event_information_and_deleted_event_preserve_period_record(planning_data, client):
    from ephios.core.models import Event

    from ephios_shift_coordination.services import create_period
    from tests.test_periods import request_data

    period = create_period(planning_data.coordinator, **request_data(planning_data))
    event = period.events.first().event
    client.force_login(planning_data.coordinator)
    assert period.get_absolute_url().encode() in client.get(event.get_absolute_url()).content
    client.force_login(planning_data.member)
    response = client.get(event.get_absolute_url())
    assert b"Shift coordination" in response.content
    assert period.get_absolute_url().encode() not in response.content
    client.force_login(planning_data.outsider)
    assert client.get(event.get_absolute_url()).status_code in (403, 404)
    event.delete()
    client.force_login(planning_data.coordinator)
    assert b"original event was deleted" in client.get(period.get_absolute_url()).content
    assert Event.objects.count() == 1


def test_navigation_and_group_permission_integration(planning_data, rf):
    from django.contrib.auth.models import AnonymousUser
    from ephios.core.signals import register_group_permission_fields

    from ephios_shift_coordination.signals import navigation, settings_links

    request = rf.get("/")
    request.user = AnonymousUser()
    assert navigation(None, request) == []
    request.user = planning_data.member
    assert settings_links(None, request) == []
    fields = [
        field for _, result in register_group_permission_fields.send(None) for field in result
    ]
    assert any(name == "manage_planning" for name, _ in fields)


def test_method_and_identifier_tampering_is_rejected(planning_data, client):
    client.force_login(planning_data.admin)
    assert client.delete(url("settings")).status_code == 405
    assert client.get(url("period_detail", 987654)).status_code == 404
    client.force_login(planning_data.member)
    assert client.post(url("template_edit", planning_data.template.pk), {}).status_code == 403


def test_foreign_shift_identifier_cannot_modify_another_template(planning_data, client):
    from ephios_shift_coordination.models import ServiceTemplate, ShiftTemplate

    other = ServiceTemplate.objects.create(
        title="Other", location="Other", event_type=planning_data.template.event_type
    )
    shift = planning_data.template.shifts.first()
    shift.pk = None
    shift.template = other
    shift.save()
    original_label = shift.label
    payload = template_payload(planning_data.template)
    payload["shifts-0-id"] = shift.pk
    payload["shifts-0-label"] = "Tampered"
    client.force_login(planning_data.admin)
    response = client.post(url("template_edit", planning_data.template.pk), payload)
    assert response.status_code == 200
    assert response.context["shifts"].non_form_errors()
    assert ShiftTemplate.objects.get(pk=shift.pk).label == original_label


def test_manually_created_events_stay_untouched(planning_data, client):
    from ephios.core.models import Event

    event = Event.objects.create(title="Members meeting", type=planning_data.template.event_type)
    event.active = True
    event.save()
    from guardian.shortcuts import assign_perm

    assign_perm("core.view_event", planning_data.group, event)
    client.force_login(planning_data.member)
    page = client.get(event.get_absolute_url())
    assert page.status_code == 200
    assert b"Shift coordination" not in page.content


def test_the_menu_sends_members_straight_to_the_surveys_and_coordinators_to_a_menu(
    planning_data, client
):
    from ephios_shift_coordination.signals import navigation

    request = RequestFactory().get("/")
    request.user = planning_data.member
    member = navigation(None, request=request)
    assert [item["label"] for item in member] == ["Surveys", "Assemblies"]
    assert not any(item.get("group") for item in member)

    request.user = planning_data.coordinator
    coordinator = navigation(None, request=request)
    # ephios draws plain entries before the menus, so all of them have to be in the menu to
    # keep surveys, periods and assemblies in that order for everybody.
    assert [item["label"] for item in coordinator] == [
        "Surveys",
        "Planning periods",
        "Assemblies",
    ]
    assert {item["group"] for item in coordinator} == {"Shift coordination"}


def test_the_menu_stays_empty_for_anonymous_visitors(planning_data, client):
    from django.contrib.auth.models import AnonymousUser

    from ephios_shift_coordination.signals import navigation

    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    assert navigation(None, request=request) == []


def period_payload(planning_data, **changes):
    return {
        **planning_data.configuration.snapshot(),
        "template": planning_data.template.pk,
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
        "creation_key": str(uuid.uuid4()),
        "reminder_days": "3",
        **changes,
    }


def test_the_reminder_for_the_next_period_is_suggested_and_kept(planning_data, client):
    client.force_login(planning_data.coordinator)
    payload = period_payload(planning_data)
    page = client.post(url("period_create"), {**payload, "action": "calendar"})
    assert page.status_code == 200
    # Two weeks before the period ends, offered without anybody typing a date.
    assert 'value="2026-10-17"' in page.content.decode()
    assert "Plan the next period in time" in page.content.decode()
    dates = ["2026-10-06", "2026-10-13"]
    created = client.post(
        url("period_create"),
        {
            **payload,
            "action": "create",
            "selection_ready": "1",
            "reminder_ready": "1",
            "remind_next": "on",
            "next_reminder_date": "2026-10-20",
            "dates": dates,
        },
    )
    assert created.status_code == 302
    assert PlanningPeriod.objects.get().next_reminder_on == date(2026, 10, 20)


def test_the_reminder_can_be_switched_off_and_is_checked_for_sense(planning_data, client):
    client.force_login(planning_data.coordinator)
    payload = period_payload(planning_data)
    common = {
        **payload,
        "action": "create",
        "selection_ready": "1",
        "reminder_ready": "1",
        "dates": ["2026-10-06"],
    }
    refused = client.post(
        url("period_create"), {**common, "remind_next": "on", "next_reminder_date": "2026-08-01"}
    )
    assert refused.status_code == 200 and not PlanningPeriod.objects.exists()
    assert "has to be in the future" in refused.content.decode()
    late = client.post(
        url("period_create"), {**common, "remind_next": "on", "next_reminder_date": "2026-11-30"}
    )
    assert late.status_code == 200 and "comes too late" in late.content.decode()
    off = client.post(url("period_create"), {**common, "next_reminder_date": ""})
    assert off.status_code == 302
    assert PlanningPeriod.objects.get().next_reminder_on is None


def test_a_broken_end_date_reports_itself_instead_of_suggesting_a_reminder(planning_data, client):
    client.force_login(planning_data.coordinator)
    page = client.post(
        url("period_create"),
        period_payload(planning_data, end_date="not-a-date", action="calendar"),
    )
    assert page.status_code == 200 and not PlanningPeriod.objects.exists()
    assert "Enter a valid date" in page.content.decode()
