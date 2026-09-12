import pytest
from django.core.management.base import CommandError
from ephios.core.models import Event, QualificationGrant, UserProfile

from ephios_shift_coordination.models import ServiceTemplate
from scripts.demo import seed_demo


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


@pytest.mark.django_db
def test_demo_import_creates_100_members_and_a_reusable_two_shift_template(settings, monkeypatch):
    monkeypatch.setenv("EPHIOS_TESTING", "1")
    settings.DEBUG = True
    settings.EMAIL_HOST = "mail"
    settings.EMAIL_PORT = 1025
    seed_demo()
    members = UserProfile.objects.filter(
        email__startswith="demo-", email__endswith="@example.invalid"
    )
    assert members.count() == 100
    assert not members.filter(is_staff=True).exists()
    assert members.get(email="demo-001@example.invalid").has_perm(
        "ephios_shift_coordination.manage_planning"
    )
    assert members.get(email="demo-003@example.invalid").check_password("demo-only-member-password")
    assert QualificationGrant.objects.filter(user__in=members).count() == 120
    template = ServiceTemplate.objects.get(title="Dienst")
    assert list(template.shifts.values_list("label", flat=True)) == ["Schicht 1", "Schicht 2"]
    template.location = "Edited demo location"
    template.save()
    seed_demo()
    assert members.count() == 100
    assert ServiceTemplate.objects.count() == 1
    template.refresh_from_db()
    assert template.location == "Edited demo location"
    assert not Event.all_objects.exists()
