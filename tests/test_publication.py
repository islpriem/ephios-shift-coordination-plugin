import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from ephios.core.models import LocalParticipation
from ephios.core.models.users import Notification
from icalendar import Calendar

from ephios_shift_coordination.drafts import save_draft, validate_draft
from ephios_shift_coordination.models import NotificationDispatch, PlanningPeriod
from ephios_shift_coordination.publication import load_publication, publish_plan, review_publication
from ephios_shift_coordination.services import Conflict
from tests.test_drafts import draft_data as draft_data
from tests.test_drafts import native_commitment, payload
from tests.test_surveys import survey_data as survey_data


def prepare(data, assignments=None):
    body = payload(data, assignments)
    checks = validate_draft(data.coordinator, data.period.pk, **body)
    saved = save_draft(
        data.coordinator,
        data.period.pk,
        **body,
        confirmations=[
            {"token": v["token"], "confirmed": True, "reason": "Explicit agreement"}
            for v in checks["violations"]
        ],
    )
    return {
        "expected_version": saved["version"],
        "fingerprint": saved["fingerprint"],
        "confirmed_tokens": [v["token"] for v in checks["violations"]],
        "confirm_underfilled": True,
        "confirm_publish": True,
    }


def test_publication_creates_native_participations_once_and_uses_existing_personal_feed(
    client, draft_data
):
    data = draft_data
    body = prepare(
        data,
        [
            [data.member.pk, s.pk]
            for s in data.period.responses.get(user=data.member).offered_shifts.all()
        ],
    )
    initial_notifications = Notification.objects.count()
    feed = reverse("core:user_event_feed", args=[data.member.calendar_token])
    assert not Calendar.from_ical(client.get(feed).content).walk("VEVENT")
    review = review_publication(data.coordinator, data.period.pk)
    assert review["version"] == body["expected_version"]
    assert not LocalParticipation.objects.exists()
    published = publish_plan(data.coordinator, data.period.pk, **body)
    assert published.state == PlanningPeriod.State.PUBLISHED
    assert published.published_by == data.coordinator and published.published_at
    assert published.version == body["expected_version"] + 1
    assert LocalParticipation.objects.count() == 2
    assert set(LocalParticipation.objects.values_list("state", flat=True)) == {
        LocalParticipation.States.CONFIRMED
    }
    assert Notification.objects.count() == initial_notifications + 1
    assert NotificationDispatch.objects.filter(kind="publication").count() == 1
    snapshot = published.publication_snapshot
    assert "private answer" not in json.dumps(snapshot) and "@example.invalid" not in json.dumps(
        snapshot
    )
    assert publish_plan(data.coordinator, data.period.pk, **body).publication_snapshot == snapshot
    assert LocalParticipation.objects.count() == 2
    assert Notification.objects.count() == initial_notifications + 1
    events = Calendar.from_ical(client.get(feed).content).walk("VEVENT")
    assert len(events) == 2 and {str(event["STATUS"]) for event in events} == {"CONFIRMED"}
    assert {event.decoded("dtstart") for event in events} == set(
        LocalParticipation.objects.values_list("start_time", flat=True)
    )
    assert not load_publication(data.coordinator, data.period.pk)["changed"]


def test_exception_and_understaffing_must_be_explicitly_reconfirmed(draft_data):
    data = draft_data
    data.period.responses.filter(user=data.member).update(maximum=0)
    body = prepare(data)
    review = review_publication(data.coordinator, data.period.pk)
    assert review["underfilled"] and review["overrides"]
    for change in (
        {"confirmed_tokens": []},
        {"confirm_underfilled": False},
        {"confirm_publish": False},
    ):
        with pytest.raises(ValidationError):
            publish_plan(data.coordinator, data.period.pk, **{**body, **change})
    assert not LocalParticipation.objects.exists()
    publish_plan(data.coordinator, data.period.pk, **body)
    assert LocalParticipation.objects.count() == 1
    assert data.period.responses.get(user=data.member).maximum == 0


def test_failure_after_first_native_save_rolls_everything_back(draft_data, monkeypatch):
    data = draft_data
    body = prepare(data)
    count = Notification.objects.count()
    original = LocalParticipation.save

    def fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Injected after native write")

    monkeypatch.setattr(LocalParticipation, "save", fail)
    with pytest.raises(RuntimeError):
        publish_plan(data.coordinator, data.period.pk, **body)
    data.period.refresh_from_db()
    assert data.period.state == PlanningPeriod.State.PLANNING
    assert data.period.version == body["expected_version"] and not data.period.publication_snapshot
    assert not LocalParticipation.objects.exists() and Notification.objects.count() == count
    assert data.period.draft_assignments.count() == 1


def test_changed_inputs_require_saving_a_freshly_checked_draft(draft_data):
    data = draft_data
    body = prepare(data)
    data.member.qualification_grants.update(
        expires=data.shifts[0].shift.end_time + timedelta(days=1)
    )
    with pytest.raises(Conflict):
        publish_plan(data.coordinator, data.period.pk, **body)
    with pytest.raises(Conflict):
        review_publication(data.coordinator, data.period.pk)
    assert not LocalParticipation.objects.exists()


def test_other_type_overlap_does_not_block_publication(draft_data):
    data = draft_data
    native_commitment(data, other_type=True)
    body = prepare(data)
    assert not body["confirmed_tokens"]
    publish_plan(data.coordinator, data.period.pk, **body)
    assert LocalParticipation.objects.count() == 2


def test_native_reassignment_changes_existing_feed_and_shows_drift_without_rewriting_snapshot(
    client, draft_data
):
    data = draft_data
    published = publish_plan(data.coordinator, data.period.pk, **prepare(data))
    snapshot = published.publication_snapshot
    participation = LocalParticipation.objects.get()
    participation.user = data.coordinator
    participation.save()
    assert load_publication(data.coordinator, data.period.pk)["changed"]
    published.refresh_from_db()
    assert published.publication_snapshot == snapshot
    for user, count in ((data.member, 0), (data.coordinator, 1)):
        feed = reverse("core:user_event_feed", args=[user.calendar_token])
        assert len(Calendar.from_ical(client.get(feed).content).walk("VEVENT")) == count


def test_publication_summary_uses_native_channels_and_current_partner_visibility(draft_data):
    from ephios.core.models import EventType
    from ephios.core.services.notifications.backends import EmailNotificationBackend

    from ephios_shift_coordination.notifications import PlanPublished

    data = draft_data
    pairs = [[person.pk, data.shifts[0].pk] for person in (data.member, data.coordinator)]
    published = publish_plan(data.coordinator, data.period.pk, **prepare(data, pairs))
    notification = Notification.objects.get(user=data.member, slug=PlanPublished.slug)
    assert notification.notification_type is PlanPublished
    assert not notification.is_obsolete
    body = str(notification.body)
    assert "coordinator" in body and "published" in body
    assert "private answer" not in body and "Explicit agreement" not in body
    assert data.member.calendar_token not in str(notification.get_actions())
    assert any(
        reverse("core:settings_calendar") in url for label, url in notification.get_actions()
    )
    with pytest.MonkeyPatch.context() as patch:
        sent = []
        patch.setattr(EmailNotificationBackend, "send", sent.append)
        EmailNotificationBackend.send_multiple([notification])
        assert sent == [notification]
        data.member.disabled_notifications = [[EmailNotificationBackend.slug, PlanPublished.slug]]
        data.member.save()
        notification.refresh_from_db()
        assert not EmailNotificationBackend.should_send(notification)
    event_type = data.template.event_type
    event_type.show_participant_data = EventType.ShowParticipantDataChoices.RESPONSIBLES
    event_type.save()
    assert "coordinator" not in PlanPublished.get_body(notification)
    participation = LocalParticipation.objects.get(user=data.member)
    participation.state = LocalParticipation.States.RESPONSIBLE_REJECTED
    participation.save()
    assert "published" in PlanPublished.get_body(notification)
    published.refresh_from_db()
    assert published.publication_snapshot["shifts"][0]["members"]


@pytest.mark.parametrize(
    "mutation",
    [
        "inactive",
        "deleted_user",
        "role",
        "permission",
        "event",
        "shift",
        "native",
        "times",
        "state",
    ],
)
def test_current_permissions_structure_and_native_targets_cannot_be_overridden(
    draft_data, mutation
):
    from django.core.exceptions import PermissionDenied
    from guardian.shortcuts import remove_perm

    data = draft_data
    body = prepare(data)
    event = data.shifts[0].shift.event
    if mutation == "inactive":
        data.member.is_active = False
        data.member.save()
    elif mutation == "deleted_user":
        data.member.delete()
    elif mutation == "role":
        remove_perm("ephios_shift_coordination.manage_planning", data.coordination)
    elif mutation == "permission":
        remove_perm("core.change_event", data.coordination, event)
        remove_perm("core.change_event", data.coordinator, event)
    elif mutation == "event":
        event.delete()
    elif mutation == "shift":
        data.shifts[0].shift.delete()
    elif mutation == "native":
        LocalParticipation.objects.create(
            user=data.member, shift=data.shifts[0].shift, state=LocalParticipation.States.REQUESTED
        )
    elif mutation == "times":
        shift = data.shifts[0].shift
        shift.start_time += timedelta(minutes=5)
        shift.save()
    else:
        data.period.state = PlanningPeriod.State.SURVEY_OPEN
        data.period.deadline += timedelta(days=1)
        data.period.save()
    with pytest.raises((Conflict, ValidationError, PermissionDenied)):
        publish_plan(data.coordinator, data.period.pk, **body)
    assert not NotificationDispatch.objects.filter(kind="publication").exists()
    data.period.refresh_from_db()
    assert not data.period.publication_snapshot


def test_all_business_exceptions_remain_publishable_with_recorded_reasons(draft_data):
    data = draft_data
    response = data.period.responses.get(user=data.member)
    response.maximum = 0
    response.save()
    response.availabilities.update(rating="unavailable")
    data.member.qualification_grants.all().delete()
    target = data.shifts[0].shift
    for _ in range(2):
        native_commitment(
            data,
            individual_start_time=target.start_time - timedelta(days=1),
            individual_end_time=target.end_time,
        )
    pairs = [
        [person.pk, data.shifts[0].pk] for person in (data.member, data.coordinator, data.admin)
    ]
    body = prepare(data, pairs)
    assert set(data.period.rule_overrides.values_list("code", flat=True)) == {
        "missing_response",
        "unavailable",
        "qualification",
        "personal_maximum",
        "weekly_limit",
        "consecutive_days",
        "overlap",
        "shift_maximum",
    }
    published = publish_plan(data.coordinator, data.period.pk, **body)
    assert (
        LocalParticipation.objects.filter(
            shift=target, state=LocalParticipation.States.CONFIRMED
        ).count()
        == 3
    )
    assert NotificationDispatch.objects.filter(kind="publication").count() == 3
    assert len(published.publication_snapshot["override_ids"]) == len(body["confirmed_tokens"])


@pytest.mark.parametrize("mutation", ["times", "deleted_shift", "deleted_user", "extra", "state"])
def test_published_record_detects_native_drift_and_survives_deletions(draft_data, mutation):
    data = draft_data
    published = publish_plan(data.coordinator, data.period.pk, **prepare(data))
    original = published.publication_snapshot
    participation = LocalParticipation.objects.get()
    if mutation == "times":
        participation.individual_start_time = participation.start_time + timedelta(minutes=5)
        participation.save()
    elif mutation == "deleted_shift":
        participation.shift.delete()
    elif mutation == "deleted_user":
        data.member.delete()
    elif mutation == "extra":
        LocalParticipation.objects.create(
            user=data.coordinator,
            shift=participation.shift,
            state=LocalParticipation.States.CONFIRMED,
        )
    else:
        participation.state = LocalParticipation.States.USER_DECLINED
        participation.save()
    result = load_publication(data.coordinator, data.period.pk)
    assert result["changed"] and result["rows"][0]["changed"]
    published.refresh_from_db()
    assert published.publication_snapshot == original


def test_notifications_roll_back_with_publication_and_sending_is_only_after_commit(
    draft_data, monkeypatch, django_capture_on_commit_callbacks
):
    from ephios_shift_coordination import publication

    data = draft_data
    body = prepare(data)
    original = Notification.save
    calls = []
    monkeypatch.setattr(publication, "send_all_notifications", lambda: calls.append("sent"))

    def fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if self.slug == "shift_coordination_published":
            raise RuntimeError("Injected notification failure")

    with monkeypatch.context() as patch:
        patch.setattr(Notification, "save", fail)
        with pytest.raises(RuntimeError):
            publish_plan(data.coordinator, data.period.pk, **body)
    assert not calls and not LocalParticipation.objects.exists()
    assert not NotificationDispatch.objects.filter(kind="publication").exists()
    with django_capture_on_commit_callbacks(execute=True):
        publish_plan(data.coordinator, data.period.pk, **body)
        assert not calls
    assert calls == ["sent"]


def test_empty_saved_plan_has_a_record_and_no_summary_recipients(draft_data):
    data = draft_data
    with pytest.raises(Conflict):
        review_publication(data.coordinator, data.period.pk)
    body = prepare(data, [])
    assert not review_publication(data.coordinator, data.period.pk)["recipients"]
    published = publish_plan(data.coordinator, data.period.pk, **body)
    assert published.state == PlanningPeriod.State.PUBLISHED
    assert not LocalParticipation.objects.exists()
    assert not NotificationDispatch.objects.filter(kind="publication").exists()
    assert not load_publication(data.coordinator, data.period.pk)["changed"]
    with pytest.raises(Conflict):
        publish_plan(data.coordinator, data.period.pk, **{**body, "fingerprint": "other"})


@pytest.mark.parametrize("change", ["inactive", "access", "shift", "period"])
def test_unavailable_summary_targets_are_obsolete_and_do_not_leak_details(draft_data, change):
    from ephios_shift_coordination.notifications import PlanPublished

    data = draft_data
    publish_plan(data.coordinator, data.period.pk, **prepare(data))
    notification = Notification.objects.get(user=data.member, slug=PlanPublished.slug)
    if change == "inactive":
        data.member.is_active = False
        data.member.save()
    elif change == "access":
        data.member.groups.clear()
    elif change == "shift":
        data.shifts[0].shift.delete()
    else:
        data.period.delete()
    notification.refresh_from_db()
    assert notification.is_obsolete
    assert not notification.get_actions()
    assert "Shift 1" not in str(notification.body)
    assert "private answer" not in str(notification.body)


def test_failure_sending_after_commit_does_not_reopen_or_duplicate_publication(
    draft_data, monkeypatch, django_capture_on_commit_callbacks
):
    from ephios_shift_coordination import publication

    data = draft_data
    body = prepare(data)

    def fail():
        raise RuntimeError("Injected sender failure")

    monkeypatch.setattr(publication, "send_all_notifications", fail)
    with django_capture_on_commit_callbacks(execute=True):
        publication.publish_plan(data.coordinator, data.period.pk, **body)
    data.period.refresh_from_db()
    assert data.period.state == PlanningPeriod.State.PUBLISHED
    assert LocalParticipation.objects.count() == 1
    assert not Notification.objects.get(slug="shift_coordination_published").processing_completed
    assert (
        publication.publish_plan(data.coordinator, data.period.pk, **body).version
        == data.period.version
    )
    assert NotificationDispatch.objects.filter(kind="publication").count() == 1
