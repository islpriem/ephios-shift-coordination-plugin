import pytest
from django.core.management.base import CommandError
from ephios.core.models import Event, LocalParticipation, QualificationGrant, UserProfile

from ephios_shift_coordination.models import PlanningPeriod, ServiceTemplate
from scripts.demo import MEMBERS, seed_demo


@pytest.mark.django_db
def test_demo_import_requires_the_isolated_mail_catcher(settings, monkeypatch):
    monkeypatch.delenv("EPHIOS_TESTING", raising=False)
    with pytest.raises(CommandError):
        seed_demo()
    monkeypatch.setenv("EPHIOS_TESTING", "1")
    settings.DEBUG = True
    settings.EMAIL_HOST = "real-smtp.invalid"
    with pytest.raises(CommandError):
        seed_demo()
    assert not ServiceTemplate.objects.exists()


@pytest.fixture
def demo(settings, monkeypatch):
    monkeypatch.setenv("EPHIOS_TESTING", "1")
    settings.DEBUG = True
    settings.EMAIL_HOST = "mail"
    settings.EMAIL_PORT = 1025
    seed_demo()
    return UserProfile.objects.filter(email__startswith="demo-", email__endswith="@example.invalid")


@pytest.mark.django_db
def test_demo_import_creates_members_and_a_reusable_two_shift_template(demo):
    assert demo.count() == MEMBERS
    assert not demo.filter(is_staff=True).exists()
    assert demo.get(email="demo-001@example.invalid").has_perm(
        "ephios_shift_coordination.manage_planning"
    )
    assert demo.get(email="demo-003@example.invalid").check_password("demo")
    assert QualificationGrant.objects.filter(user__in=demo).count() == MEMBERS + MEMBERS // 5
    template = ServiceTemplate.objects.get(title="Dienst")
    assert list(template.shifts.values_list("label", flat=True)) == ["Schicht 1", "Schicht 2"]
    template.location = "Edited demo location"
    template.save()
    periods = list(PlanningPeriod.objects.values_list("pk", flat=True))
    seed_demo()
    assert demo.count() == MEMBERS and ServiceTemplate.objects.count() == 1
    template.refresh_from_db()
    assert template.location == "Edited demo location"
    # A second import neither duplicates the periods nor touches their state.
    assert list(PlanningPeriod.objects.values_list("pk", flat=True)) == periods


@pytest.mark.django_db
def test_demo_import_covers_every_state_a_coordinator_wants_to_try(demo):
    periods = list(PlanningPeriod.objects.order_by("start_date"))
    assert [period.effective_state for period in periods] == [
        PlanningPeriod.State.PUBLISHED,
        PlanningPeriod.State.PUBLISHED,
        PlanningPeriod.State.PLANNING,
        PlanningPeriod.State.SURVEY_OPEN,
    ]
    past, upcoming, planning, running = periods
    assert past.end_date < upcoming.start_date
    assert LocalParticipation.objects.filter(shift__event__planning_link__period=past).exists()
    # The closed survey is the one to practise planning with: answers but no plan yet.
    assert planning.responses.filter(submitted_at__isnull=False).count() > MEMBERS * 0.8
    assert not planning.draft_assignments.exists()
    # The running survey is deliberately incomplete, and some people left a note.
    answered = running.responses.filter(submitted_at__isnull=False).count()
    assert 0 < answered < running.responses.count()
    assert running.responses.exclude(notes="").exists()
    assert Event.all_objects.filter(planning_link__period__in=periods).exists()
