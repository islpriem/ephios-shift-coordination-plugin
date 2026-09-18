"""Synthetic demo data for the isolated development stack: never for production."""

import os
import random
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import (
    EventType,
    Qualification,
    QualificationCategory,
    QualificationGrant,
    UserProfile,
)
from guardian.shortcuts import assign_perm

from ephios_shift_coordination.assemblies import answer as answer_assembly
from ephios_shift_coordination.assemblies import plan_assembly
from ephios_shift_coordination.drafts import load_plan, save_draft
from ephios_shift_coordination.models import (
    PlanningPeriod,
    PlanningSettings,
    ServiceTemplate,
    ShiftTemplate,
)
from ephios_shift_coordination.proposals import create_proposal
from ephios_shift_coordination.publication import publish_plan
from ephios_shift_coordination.services import create_period
from ephios_shift_coordination.surveys import close_survey, open_survey, save_response

PASSWORD = "demo"
MEMBERS = 30
PERIOD_DAYS = 14
NOTES = [
    "Am liebsten zusammen mit Demoperson 004.",
    "Ich kann kurzfristig einspringen, ruft mich einfach an.",
    "Bitte nicht zwei Wochenenden hintereinander einteilen.",
    "Ich bringe eine Person in Ausbildung mit.",
]


def guarded():
    if not (
        os.environ.get("EPHIOS_TESTING") == "1"
        and settings.DEBUG
        and settings.EMAIL_HOST == "mail"
        and settings.EMAIL_PORT == 1025
    ):
        raise CommandError(
            "Demo import requires the isolated development stack and its mail catcher."
        )


@contextmanager
def pretend_now(moment):
    """Run a whole period lifecycle at an earlier point in time, for demo history."""
    original = timezone.now
    timezone.now = lambda: moment
    try:
        yield
    finally:
        timezone.now = original


def people(count=MEMBERS):
    """Create or reuse demo members; the first two also coordinate."""
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
    password = make_password(PASSWORD)
    created = []
    for index in range(1, count + 1):
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
        # Every fifth person holds both qualifications, the others alternate.
        for skill in skills if index % 5 == 0 else [skills[(index - 1) % 2]]:
            QualificationGrant.objects.get_or_create(user=user, qualification=skill)
        created.append(user)
    return created, members, coordinators, skills


def service_template(members, coordinators, skills):
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
    return template


def dates_of(start):
    """Every second day of a fortnight, so a demo period holds seven services."""
    return [start + timedelta(days=offset) for offset in range(0, PERIOD_DAYS, 2)]


def build_period(coordinator, template, start, title):
    return create_period(
        coordinator,
        template_id=template.pk,
        start_date=start,
        end_date=start + timedelta(days=PERIOD_DAYS - 1),
        dates=dates_of(start),
        rules=PlanningSettings.objects.get(pk=1).snapshot(),
        creation_key=uuid.uuid5(uuid.NAMESPACE_DNS, f"demo-period-{title}"),
    )


def answer(user, period, rng, *, scarce_dates=(), notes="", sitting=False):
    """One complete survey answer with a plausible mix over the rating scale."""
    response = period.responses.get(user=user)
    ratings, wished = {}, []
    for planned in response.offered_shifts.select_related("event").all():
        if planned.event.date in scarce_dates:
            rating = "unavailable" if rng.random() < 0.85 else "if_needed"
        else:
            rating = rng.choices(
                ["unavailable", "if_needed", "available", "preferred"], [2, 2, 4, 2]
            )[0]
        ratings[planned.pk] = rating
    if sitting and period.rules.get("allow_observers"):
        wished = [link.pk for link in period.events.all() if rng.random() < 0.3]
    return save_response(
        user,
        period.pk,
        expected_version=response.version,
        maximum=rng.randint(2, 4),
        notes=notes,
        ratings=ratings,
        observer_events=wished,
    )


def answer_all(period, cohort, rng, *, share=1.0, scarce_dates=()):
    for index, user in enumerate(cohort):
        if rng.random() > share:
            continue
        answer(
            user,
            period,
            rng,
            scarce_dates=scarce_dates,
            notes=NOTES[index % len(NOTES)] if index % 7 == 0 else "",
            sitting=index % 3 == 0,
        )


def run_survey(coordinator, period, *, deadline, reminders, cohort, rng, share, scarce=()):
    """Open the survey, collect the demo answers and close it again."""
    period.refresh_from_db()
    open_survey(
        coordinator,
        period.pk,
        expected_version=period.version,
        deadline=deadline,
        reminder_days=reminders,
        maximum=3,
    )
    period.refresh_from_db()
    answer_all(period, cohort, rng, share=share, scarce_dates=scarce)
    return period


def close(coordinator, period):
    period.refresh_from_db()
    close_survey(coordinator, period.pk, expected_version=period.version)
    period.refresh_from_db()
    return period


def plan_and_publish(coordinator, period):
    """Take the optimizer proposal, save it as the shared draft and publish it."""
    plan = load_plan(coordinator, period.pk)
    proposal = create_proposal(
        coordinator, period.pk, expected_version=plan["version"], fingerprint=plan["fingerprint"]
    )
    saved = save_draft(
        coordinator,
        period.pk,
        expected_version=plan["version"],
        fingerprint=plan["fingerprint"],
        # The service returns tuples; the saved draft takes the same shape as the browser sends.
        assignments=[list(pair) for pair in proposal["assignments"]],
        observers=[list(pair) for pair in proposal.get("observers", [])],
        confirmations=[],
    )
    checks = load_plan(coordinator, period.pk)
    return publish_plan(
        coordinator,
        period.pk,
        expected_version=saved["version"],
        fingerprint=saved["fingerprint"],
        confirmed_tokens=[violation["token"] for violation in checks["violations"]],
        confirm_underfilled=True,
        confirm_publish=True,
    )


def midday(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


@transaction.atomic
def assembly_type(members, coordinators):
    """An event type that stands for assemblies, with the defaults a coordinator starts from."""
    event_type, _ = EventType.objects.get_or_create(
        title="Mitgliederversammlung",
        defaults={"default_description": "Versammlung aller Mitglieder."},
    )
    event_type.preferences["shift_coordination__is_assembly"] = True
    event_type.preferences["shift_coordination__assembly_title"] = "Mitgliederversammlung"
    event_type.preferences["shift_coordination__assembly_location"] = "Demo room"
    event_type.preferences["visible_for"] = [members]
    event_type.preferences["responsible_groups"] = [coordinators]
    return event_type


def call_assembly(coordinator, event_type, cohort, day, rng):
    """One called and invited assembly, so the whole flow can be tried straight away."""
    start = midday(day) + timedelta(hours=7)
    assembly = plan_assembly(
        coordinator,
        event_type=event_type,
        title="Mitgliederversammlung",
        description="Versammlung aller Mitglieder.",
        location="Demo room",
        start=start,
        end=start + timedelta(hours=2),
        agenda="Bericht des Vorstands\nPlanung des nächsten Quartals\nVerschiedenes",
        silent=False,
    )
    for person in cohort:
        if (choice := rng.random()) < 0.5:
            answer_assembly(assembly, person, attending=choice < 0.35)
    return assembly


def seed_demo():
    guarded()
    cohort, members, coordinators, skills = people()
    template = service_template(members, coordinators, skills)
    settings_row, _ = PlanningSettings.objects.get_or_create(pk=1)
    if not settings_row.allow_observers:
        settings_row.allow_observers = True
        settings_row.service_reminder_days = [1]
        settings_row.assembly_reminder_days = [3, 1]
        settings_row.save()
    template.event_type.preferences["shift_coordination__is_service"] = True
    meetings = assembly_type(members, coordinators)
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = sorted(
        set([*preferences["general__enabled_plugins"], "ephios_shift_coordination"])
    )
    if PlanningPeriod.objects.exists():
        return template
    coordinator = cohort[0]
    rng = random.Random(20260916)
    today = timezone.localdate()

    # 1. A finished period: asked, planned and published before it started.
    past_start = today - timedelta(days=PERIOD_DAYS + 14)
    with pretend_now(midday(past_start - timedelta(days=10))):
        past = build_period(coordinator, template, past_start, "past")
        run_survey(
            coordinator,
            past,
            deadline=midday(past_start - timedelta(days=3)),
            reminders=[3],
            cohort=cohort,
            rng=rng,
            share=0.8,
        )
        plan_and_publish(coordinator, close(coordinator, past))

    # 2. A published period that is about to start.
    upcoming = build_period(coordinator, template, today + timedelta(days=7), "upcoming")
    run_survey(
        coordinator,
        upcoming,
        deadline=midday(today + timedelta(days=3)),
        reminders=[2],
        cohort=cohort,
        rng=rng,
        share=0.85,
    )
    plan_and_publish(coordinator, close(coordinator, upcoming))

    # 3. Answers complete, survey closed: this one is waiting to be planned. Its last two
    # services deliberately lack people, so understaffing is visible in the demo too.
    planning_start = today + timedelta(days=28)
    planning = build_period(coordinator, template, planning_start, "planning")
    run_survey(
        coordinator,
        planning,
        deadline=midday(today + timedelta(days=5)),
        reminders=[2],
        cohort=cohort,
        rng=rng,
        share=0.95,
        scarce=set(dates_of(planning_start)[-2:]),
    )
    close(coordinator, planning)

    # 4. A survey that is still running and only half answered.
    running = build_period(coordinator, template, today + timedelta(days=49), "running")
    run_survey(
        coordinator,
        running,
        deadline=midday(today + timedelta(days=12)),
        reminders=[3, 1],
        cohort=cohort,
        rng=rng,
        share=0.5,
    )

    # 5. One assembly that has been called and invited, with about half the answers in.
    call_assembly(coordinator, meetings, cohort, today + timedelta(days=21), rng)
    return template
