"""Synthetic load fixtures, executed only in the guarded local test application."""

import json
import os
import platform
import random
import uuid
from datetime import date, time, timedelta
from time import monotonic

import scipy
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ephios.core.models import EventType, Qualification, QualificationGrant, UserProfile

from ephios_shift_coordination.drafts import snapshot
from ephios_shift_coordination.ephios_integration import check_structure
from ephios_shift_coordination.models import (
    Availability,
    PlanningPeriod,
    PlanningSettings,
    ServiceTemplate,
    ShiftTemplate,
    SurveyResponse,
)
from ephios_shift_coordination.services import create_period


def load_fixture(period_id=None, scarce=False):
    assert os.environ.get("EPHIOS_TESTING") == "1"
    coordinator = UserProfile.objects.get(email="demo-001@example.invalid")
    # The agreed load case is 100 people; the demo import itself stays deliberately small.
    members = Group.objects.get(name="Demo members")
    skills = [
        Qualification.objects.get(
            uuid=uuid.uuid5(uuid.NAMESPACE_DNS, f"demo-skill-{index}.example.invalid")
        )
        for index in (1, 2)
    ]
    for index in range(1, 101):
        person, created = UserProfile.all_objects.get_or_create(
            email=f"demo-{index:03}@example.invalid",
            defaults={
                "display_name": f"Demoperson {index:03}",
                "date_of_birth": date(1990, 1, 1),
                "is_active": True,
                "preferred_language": "de",
            },
        )
        if created:
            person.groups.add(members)
            for skill in skills if index % 5 == 0 else [skills[(index - 1) % 2]]:
                QualificationGrant.objects.get_or_create(user=person, qualification=skill)
    if period_id is None:
        template = ServiceTemplate.objects.create(
            title=f"Load fixture {uuid.uuid4()}",
            location="Synthetic load test",
            event_type=EventType.objects.create(title=f"Load type {uuid.uuid4()}"),
        )
        template.visible_for.add(Group.objects.get(name="Demo members"))
        template.responsible_groups.add(Group.objects.get(name="Demo coordinators"))
        for index in range(3):
            shift = ShiftTemplate.objects.create(
                template=template,
                position=index,
                label=f"Load shift {index + 1}",
                meeting_time=time(8 + 3 * index),
                start_time=time(8 + 3 * index),
                end_time=time(11 + 3 * index),
                minimum=2,
                maximum=3,
            )
            shift.qualifications.add(
                Qualification.objects.get(
                    uuid=uuid.uuid5(
                        uuid.NAMESPACE_DNS, f"demo-skill-{index % 2 + 1}.example.invalid"
                    )
                )
            )
        period = create_period(
            coordinator,
            template_id=template.pk,
            start_date=date(2034, 1, 1),
            end_date=date(2034, 3, 31),
            dates=[date(2034, 1, 1) + timedelta(days=index * 89 // 66) for index in range(67)],
            rules=PlanningSettings.objects.get(pk=1).snapshot(),
            creation_key=uuid.uuid4(),
        )
        _, period.opened_structure = check_structure(period, coordinator)
        period.opened_at = timezone.now() - timedelta(days=8)
        period.deadline = timezone.now() - timedelta(days=1)
        period.state = PlanningPeriod.State.PLANNING
        period.save()
    else:
        period = PlanningPeriod.objects.get(pk=period_id)
        period.responses.all().delete()
    with transaction.atomic():
        initial = snapshot(coordinator, period.pk)
    offered = {}
    for answer in initial.data.availability:
        if answer.eligible:
            offered.setdefault(answer.person_id, []).append(answer.shift_id)
    rng = random.Random(20260915)
    people = list(
        UserProfile.objects.filter(email__regex=r"^demo-[0-9]{3}@example.invalid$").order_by(
            "email"
        )
    )
    assert len(people) == 100
    now = timezone.now()
    responses = SurveyResponse.objects.bulk_create(
        [
            SurveyResponse(
                period=period,
                user=person,
                maximum=(rng.randint(1, 3) if scarce else rng.randint(5, 9))
                if index % (4 if scarce else 23) != (1 if scarce else 0)
                else None,
                submitted_at=now if index % (4 if scarce else 23) != (1 if scarce else 0) else None,
                version=1,
            )
            for index, person in enumerate(people)
        ]
    )
    # Scarcity deliberately leaves only a quarter of people with complete responses.
    if scarce:
        for index, response in enumerate(responses):
            if index % 4:
                response.maximum = response.submitted_at = None
        SurveyResponse.objects.bulk_update(responses, ["maximum", "submitted_at"])
    SurveyResponse.offered_shifts.through.objects.bulk_create(
        [
            SurveyResponse.offered_shifts.through(
                surveyresponse_id=response.pk, plannedshift_id=sid
            )
            for response in responses
            for sid in offered.get(response.user_id, [])
        ]
    )
    Availability.objects.bulk_create(
        [
            Availability(
                response=response,
                planned_shift_id=sid,
                rating=rng.choices(
                    ["unavailable", "if_needed", "available", "preferred"],
                    weights=[20, 65, 10, 5] if scarce else [20, 10, 55, 15],
                )[0],
            )
            for response in responses
            if response.submitted_at
            for sid in offered.get(response.user_id, [])
        ]
    )
    started = monotonic()
    # The query log is capped, so building the fixture would make the measurement meaningless.
    connection.queries_log.clear()
    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        measured = snapshot(coordinator, period.pk)
    query_seconds = monotonic() - started
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        database = cursor.fetchone()[0]
    print(
        json.dumps(
            {
                "period": period.pk,
                "case": "scarce" if scarce else "realistic",
                "people": len(people),
                "shifts": len(measured.data.shifts),
                "complete": sum(person.complete for person in measured.data.people),
                "snapshot_queries": len(queries),
                "snapshot_seconds": query_seconds,
                "database": database,
                "python": platform.python_version(),
                "scipy": scipy.__version__,
                "platform": platform.platform(),
                "cpus": os.cpu_count(),
            }
        )
    )
