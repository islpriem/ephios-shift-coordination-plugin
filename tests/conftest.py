from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import EventType, Qualification, QualificationCategory, UserProfile
from guardian.shortcuts import assign_perm

from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate, ShiftTemplate


@pytest.fixture(autouse=True)
def clear_preference_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def planning_data(db, monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 9, 1, tzinfo=UTC))
    preferences = global_preferences_registry.manager()
    original = preferences["general__enabled_plugins"]
    preferences["general__enabled_plugins"] = sorted(set([*original, "ephios_shift_coordination"]))
    group = Group.objects.create(name="Arbitrary members")
    coordination = Group.objects.create(name="Arbitrary coordinators")
    users = {}
    for role in ("admin", "coordinator", "member", "outsider"):
        users[role] = UserProfile.objects.create(
            email=f"{role}@example.invalid",
            display_name=role,
            date_of_birth=date(1990, 1, 1),
            is_staff=role == "admin",
            is_superuser=role == "admin",
            is_active=True,
        )
        if role != "outsider":
            users[role].groups.add(group)
    users["coordinator"].groups.add(coordination)
    assign_perm("ephios_shift_coordination.manage_planning", coordination)
    assign_perm("core.add_event", coordination)
    assign_perm("publish_event_for_group", coordination, group)
    event_type = EventType.objects.create(title="Duty")
    template = ServiceTemplate.objects.create(
        title="Duty", location="Test location", event_type=event_type
    )
    template.visible_for.add(group)
    template.responsible_groups.add(coordination)
    category = QualificationCategory.objects.create(title="Test skills")
    for index in (1, 2):
        skill = Qualification.objects.create(
            title=f"Skill {index}", abbreviation=f"S{index}", category=category
        )
        shift = ShiftTemplate.objects.create(
            template=template,
            label=f"Shift {index}",
            position=index,
            meeting_time=time(8 + 4 * index, 45),
            start_time=time(9 + 4 * index),
            end_time=time(13 + 4 * index),
            minimum=index,
            maximum=index + 1,
        )
        shift.qualifications.add(skill)
    configuration = PlanningSettings.objects.create(region="BE")
    yield SimpleNamespace(
        **users,
        group=group,
        coordination=coordination,
        template=template,
        configuration=configuration,
    )
    preferences["general__enabled_plugins"] = original


@pytest.fixture
def assembly_type(planning_data):
    """An event type marked as an assembly, with the defaults a coordinator starts from."""
    event_type = EventType.objects.create(
        title="Team meeting", default_description="As every month"
    )
    event_type.preferences["shift_coordination__is_assembly"] = True
    event_type.preferences["shift_coordination__assembly_title"] = "Monthly meeting"
    event_type.preferences["shift_coordination__assembly_location"] = "Back room"
    event_type.preferences["visible_for"] = [planning_data.group]
    event_type.preferences["responsible_groups"] = [planning_data.coordination]
    return event_type


_rendered_templates = set()


def pytest_configure():
    from django.test.signals import template_rendered

    template_rendered.connect(_track_template, dispatch_uid="planning_test_templates", weak=False)


def _track_template(sender, template, **kwargs):
    if template.origin:
        _rendered_templates.add(str(template.origin.name))


def pytest_terminal_summary(terminalreporter):
    import json
    from pathlib import Path

    if terminalreporter.config.option.markexpr in ("e2e", "postgres"):
        return

    templates = Path("src/ephios_shift_coordination/templates").resolve()
    all_files = sorted(templates.rglob("*.html"))
    rendered = [
        str(path.relative_to(templates)) for path in all_files if str(path) in _rendered_templates
    ]
    missing = [
        str(path.relative_to(templates))
        for path in all_files
        if str(path) not in _rendered_templates
    ]
    report = {
        "measurement": "Rendered templates: file coverage, not branch coverage",
        "rendered": rendered,
        "not_rendered": missing,
    }
    target = Path(".local/test-results/template-coverage.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    terminalreporter.write_sep(
        "-", f"Plugin templates rendered: {len(rendered)}/{len(all_files)} (file coverage)"
    )
