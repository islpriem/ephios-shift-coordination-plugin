import os
import uuid
from datetime import date, time

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.management.base import CommandError
from django.db import transaction
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import (
    EventType,
    Qualification,
    QualificationCategory,
    QualificationGrant,
    UserProfile,
)
from guardian.shortcuts import assign_perm

from ephios_shift_coordination.models import PlanningSettings, ServiceTemplate, ShiftTemplate


@transaction.atomic
def seed_demo():
    if not (
        os.environ.get("EPHIOS_TESTING") == "1"
        and settings.DEBUG
        and settings.EMAIL_HOST == "mail"
        and settings.EMAIL_PORT == 1025
    ):
        raise CommandError(
            "Demo import requires the isolated development stack and its mail catcher."
        )
    members, _ = Group.objects.get_or_create(name="Demo members")
    coordinators, _ = Group.objects.get_or_create(name="Demo coordinators")
    assign_perm("ephios_shift_coordination.manage_planning", coordinators)
    assign_perm("core.add_event", coordinators)
    assign_perm("publish_event_for_group", coordinators, members)
    category, _ = QualificationCategory.objects.get_or_create(
        uuid=uuid.uuid5(uuid.NAMESPACE_DNS, "demo-skills.example.invalid"),
        defaults={"title": "Demo skills"},
    )
    skills = []
    for index in (1, 2):
        skill, _ = Qualification.objects.get_or_create(
            uuid=uuid.uuid5(uuid.NAMESPACE_DNS, f"demo-skill-{index}.example.invalid"),
            defaults={
                "title": f"Demo Schicht {index}",
                "abbreviation": f"DS{index}",
                "category": category,
                "is_imported": False,
            },
        )
        skills.append(skill)
    password = make_password("demo-only-member-password")
    for index in range(1, 101):
        user, _ = UserProfile.all_objects.get_or_create(
            email=f"demo-{index:03}@example.invalid",
            defaults={
                "display_name": f"Demoperson {index:03}",
                "date_of_birth": date(1990, 1, 1),
                "is_active": True,
                "preferred_language": "de",
                "password": password,
            },
        )
        user.groups.add(members)
        if index <= 2:
            user.groups.add(coordinators)
        for skill in skills if index % 5 == 0 else [skills[(index - 1) % 2]]:
            QualificationGrant.objects.get_or_create(user=user, qualification=skill)
    event_type, _ = EventType.objects.get_or_create(title="Dienst")
    template, created = ServiceTemplate.objects.get_or_create(
        title="Dienst",
        defaults={
            "description": "Synthetic demo service",
            "location": "Demo room",
            "event_type": event_type,
        },
    )
    if created:
        template.visible_for.add(members)
        template.responsible_groups.add(coordinators)
        for index, start, end in [(1, 9, 13), (2, 13, 17)]:
            shift = ShiftTemplate.objects.create(
                template=template,
                label=f"Schicht {index}",
                position=index,
                meeting_time=time(start - 1, 45),
                start_time=time(start),
                end_time=time(end),
                minimum=2,
                maximum=3,
            )
            shift.qualifications.add(skills[index - 1])
    PlanningSettings.objects.get_or_create(pk=1)
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = sorted(
        set([*preferences["general__enabled_plugins"], "ephios_shift_coordination"])
    )
    return template
