from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from ephios.core.models import LocalParticipation
from ephios.core.models.users import Notification

from ephios_shift_coordination.drafts import load_plan, save_draft, validate_draft
from ephios_shift_coordination.models import DraftAssignment, RuleOverride
from ephios_shift_coordination.services import Conflict
from ephios_shift_coordination.surveys import save_response
from tests.test_surveys import answer, open_for
from tests.test_surveys import survey_data as survey_data


@pytest.fixture
def draft_data(survey_data, monkeypatch):
    data = survey_data
    data.period = open_for(data)
    response = data.period.responses.get(user=data.member)
    save_response(data.member, data.period.pk, **{**answer(response), "maximum": 2})
    monkeypatch.setattr("django.utils.timezone.now", lambda: data.period.deadline)
    return data


def payload(data, assignments=None):
    plan = load_plan(data.coordinator, data.period.pk)
    return {
        "expected_version": plan["version"],
        "fingerprint": plan["fingerprint"],
        "assignments": assignments
        if assignments is not None
        else [[data.member.pk, data.shifts[0].pk]],
    }


def test_saved_draft_is_shared_versioned_and_has_no_native_effect(draft_data):
    data = draft_data
    initial = payload(data)
    notifications = Notification.objects.count()
    result = save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    assert result["version"] == initial["expected_version"] + 1
    assert load_plan(data.admin, data.period.pk)["assignments"] == initial["assignments"]
    assert DraftAssignment.objects.count() == 1
    assert not LocalParticipation.objects.exists()
    assert Notification.objects.count() == notifications
    with pytest.raises(Conflict):
        save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    assert DraftAssignment.objects.count() == 1


def test_overrides_require_every_current_fact_and_preserve_answer_and_history(draft_data):
    data = draft_data
    response = data.period.responses.get(user=data.member)
    response.maximum = 0
    response.save()
    response.availabilities.update(rating="unavailable")
    initial = payload(data)
    validation = validate_draft(data.coordinator, data.period.pk, **initial)
    assert {v["code"] for v in validation["violations"]} == {"unavailable", "personal_maximum"}
    with pytest.raises(ValidationError):
        save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    confirmations = [
        {"token": v["token"], "confirmed": True, "reason": "Agreed exception"}
        for v in validation["violations"]
    ]
    save_draft(data.coordinator, data.period.pk, **initial, confirmations=confirmations)
    assert RuleOverride.objects.count() == 2
    response.refresh_from_db()
    assert response.maximum == 0 and response.availabilities.first().rating == "unavailable"
    assert response.notes == "<script>private answer</script>"
    save_draft(data.coordinator, data.period.pk, **payload(data, []), confirmations=[])
    assert not DraftAssignment.objects.exists() and RuleOverride.objects.count() == 2


def test_get_after_deadline_does_not_persist_state(draft_data):
    data = draft_data
    load_plan(data.coordinator, data.period.pk)
    data.period.refresh_from_db()
    assert data.period.state == data.period.State.SURVEY_OPEN
    assert data.period.deadline == timezone.now()


def test_changed_grant_fingerprint_is_conflict_even_if_eligibility_unchanged(draft_data):
    data = draft_data
    initial = payload(data)
    grant = data.member.qualification_grants.get()
    grant.expires = data.shifts[0].shift.end_time + timedelta(days=100)
    grant.save()
    with pytest.raises(Conflict):
        save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    assert not DraftAssignment.objects.exists()


def native_commitment(data, *, other_type=False, **times):
    from ephios.core.models import Event, EventType, Shift

    target = data.shifts[0].shift
    event = Event.objects.create(
        title="Private external event",
        type=(
            EventType.objects.create(title="Other type") if other_type else data.template.event_type
        ),
    )
    native = Shift.objects.create(
        event=event,
        meeting_time=target.meeting_time,
        start_time=target.start_time,
        end_time=target.end_time,
        signup_flow_slug="manual",
        structure_slug="uniform",
        structure_configuration=target.structure_configuration,
    )
    return LocalParticipation.objects.create(
        user=data.member, shift=native, state=LocalParticipation.States.CONFIRMED, **times
    )


def test_native_effective_times_long_overlaps_other_types_and_private_facts(draft_data):
    import json

    data = draft_data
    initial = payload(data)
    native_commitment(data, other_type=True)
    assert payload(data)["fingerprint"] == initial["fingerprint"]
    target = data.shifts[0].shift
    commitment = native_commitment(
        data,
        individual_start_time=target.start_time - timedelta(days=100),
        individual_end_time=target.end_time,
    )
    updated = payload(data)
    assert updated["fingerprint"] != initial["fingerprint"]
    result = validate_draft(data.coordinator, data.period.pk, **updated)
    assert {v["code"] for v in result["violations"]} == {"overlap"}
    assert result["counts"][data.member.pk]["existing"] == 0
    public = json.dumps(load_plan(data.coordinator, data.period.pk)) + json.dumps(result)
    assert "Private external event" not in public
    assert commitment.individual_start_time.isoformat() not in public
    assert "facts" not in public
    commitment.individual_start_time = target.end_time
    commitment.individual_end_time = target.end_time + timedelta(hours=2)
    commitment.save()
    assert not validate_draft(data.coordinator, data.period.pk, **payload(data))["violations"]


@pytest.mark.parametrize(
    "mutation", ["inactive", "access", "coordinator", "disabled", "structure", "native"]
)
def test_current_permissions_activity_and_structure_cannot_be_overridden(draft_data, mutation):
    from django.core.exceptions import PermissionDenied
    from dynamic_preferences.registries import global_preferences_registry
    from guardian.shortcuts import remove_perm

    data = draft_data
    initial = payload(data)
    if mutation == "inactive":
        data.member.is_active = False
        data.member.save()
    elif mutation == "access":
        data.member.groups.clear()
    elif mutation == "coordinator":
        remove_perm("ephios_shift_coordination.manage_planning", data.coordination)
    elif mutation == "disabled":
        prefs = global_preferences_registry.manager()
        prefs["general__enabled_plugins"] = [
            p for p in prefs["general__enabled_plugins"] if p != "ephios_shift_coordination"
        ]
    elif mutation == "structure":
        shift = data.shifts[0].shift
        shift.start_time += timedelta(minutes=1)
        shift.save()
    else:
        LocalParticipation.objects.create(
            user=data.member, shift=data.shifts[0].shift, state=LocalParticipation.States.CONFIRMED
        )
    with pytest.raises((Conflict, PermissionDenied)):
        save_draft(
            data.coordinator,
            data.period.pk,
            **initial,
            confirmations=[
                {"token": "invented", "confirmed": True, "reason": "Cannot override access"}
            ],
        )
    assert not DraftAssignment.objects.exists() and not RuleOverride.objects.exists()


def test_lost_native_coordinator_permission_is_denied_even_with_plugin_role(draft_data):
    from django.core.exceptions import PermissionDenied
    from guardian.shortcuts import remove_perm

    data = draft_data
    for link in data.period.events.all():
        remove_perm("core.change_event", data.coordination, link.event)
        remove_perm("core.change_event", data.coordinator, link.event)
    with pytest.raises(PermissionDenied):
        load_plan(data.coordinator, data.period.pk)


@pytest.mark.parametrize("assignments", [None, [None], [[True, 1]], [[1]], [[1, 2, 3]]])
def test_malformed_selection_is_rejected(draft_data, assignments):
    data = draft_data
    with pytest.raises(ValidationError):
        validate_draft(
            data.coordinator, data.period.pk, **{**payload(data), "assignments": assignments}
        )


def test_duplicate_selection_and_invisible_manual_person_are_rejected(draft_data):
    data = draft_data
    for assignments in (
        [[data.member.pk, data.shifts[0].pk]] * 2,
        [[data.outsider.pk, data.shifts[0].pk]],
    ):
        with pytest.raises(ValidationError):
            save_draft(
                data.coordinator, data.period.pk, **payload(data, assignments), confirmations=[]
            )


@pytest.mark.parametrize(
    "confirmation",
    [
        None,
        [None],
        [{}],
        [{"token": "x", "confirmed": False, "reason": "Reason"}],
        [{"token": "x", "confirmed": True, "reason": 1}],
        [{"token": "x", "confirmed": True, "reason": " "}],
        [{"token": "x", "confirmed": True, "reason": "x" * 2001}],
        [{"token": "x", "confirmed": True, "reason": "Reason"}],
        [{"token": "x", "confirmed": True, "reason": "Reason"}] * 2,
    ],
)
def test_malformed_or_invented_confirmations_do_not_write(draft_data, confirmation):
    data = draft_data
    with pytest.raises(ValidationError):
        save_draft(data.coordinator, data.period.pk, **payload(data), confirmations=confirmation)
    assert not DraftAssignment.objects.exists()


def test_removed_user_stays_visible_as_invalid_until_assignment_removed(draft_data):
    data = draft_data
    initial = payload(data)
    save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    data.member.delete()
    plan = load_plan(data.coordinator, data.period.pk)
    assert plan["invalid_assignments"] == initial["assignments"]
    assert plan["assignments"] == initial["assignments"]
    with pytest.raises(ValidationError):
        save_draft(
            data.coordinator,
            data.period.pk,
            **payload(data, initial["assignments"]),
            confirmations=[],
        )
    save_draft(data.coordinator, data.period.pk, **payload(data, []), confirmations=[])
    assert not DraftAssignment.objects.exists()


def test_failed_assignment_or_audit_write_rolls_back_whole_draft(draft_data, monkeypatch):
    data = draft_data
    initial = payload(data)
    save_draft(data.coordinator, data.period.pk, **initial, confirmations=[])
    version = load_plan(data.coordinator, data.period.pk)["version"]

    def fail(*args, **kwargs):
        raise RuntimeError("Audit write failed")

    monkeypatch.setattr(RuleOverride.objects, "bulk_create", fail)
    with pytest.raises(RuntimeError):
        save_draft(data.coordinator, data.period.pk, **payload(data, []), confirmations=[])
    result = load_plan(data.coordinator, data.period.pk)
    assert result["version"] == version and result["assignments"] == initial["assignments"]


def test_changed_violation_cannot_reuse_confirmation(draft_data):
    data = draft_data
    response = data.period.responses.get(user=data.member)
    response.maximum = 0
    response.save()
    initial = payload(data)
    result = validate_draft(data.coordinator, data.period.pk, **initial)
    confirmations = [
        {"token": v["token"], "confirmed": True, "reason": "Original facts"}
        for v in result["violations"]
    ]
    new = [
        [data.member.pk, s.pk]
        for s in data.period.responses.get(user=data.member).offered_shifts.all()
    ]
    with pytest.raises(ValidationError):
        save_draft(
            data.coordinator,
            data.period.pk,
            **{**initial, "assignments": new},
            confirmations=confirmations,
        )
    assert not RuleOverride.objects.exists()


def test_every_business_rule_can_be_explicitly_overridden(draft_data):
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
    assignments = [
        [person.pk, data.shifts[0].pk] for person in (data.member, data.coordinator, data.admin)
    ]
    initial = payload(data, assignments)
    result = validate_draft(data.coordinator, data.period.pk, **initial)
    assert {v["code"] for v in result["violations"]} == {
        "missing_response",
        "unavailable",
        "qualification",
        "personal_maximum",
        "weekly_limit",
        "consecutive_days",
        "overlap",
        "shift_maximum",
    }
    confirmations = [
        {"token": v["token"], "confirmed": True, "reason": "Agreed with person"}
        for v in result["violations"]
    ]
    save_draft(data.coordinator, data.period.pk, **initial, confirmations=confirmations)
    assert DraftAssignment.objects.count() == 3
    assert RuleOverride.objects.count() == len(result["violations"])
    assert set(RuleOverride.objects.values_list("actor_id", flat=True)) == {data.coordinator.pk}
    assert not LocalParticipation.objects.filter(shift=target).exists()


def test_partial_event_visibility_is_enforced_per_assignment(draft_data):
    from guardian.shortcuts import remove_perm

    data = draft_data
    remove_perm("core.view_event", data.group, data.shifts[0].shift.event)
    current = load_plan(data.coordinator, data.period.pk)
    assert data.member.pk in {p["id"] for p in current["people"]}
    assert not any(
        a["person_id"] == data.member.pk and a["shift_id"] == data.shifts[0].pk
        for a in current["availability"]
    )
    with pytest.raises(ValidationError):
        validate_draft(data.coordinator, data.period.pk, **payload(data))


def test_native_individual_start_uses_period_timezone_for_day_and_week(draft_data):
    from datetime import UTC, datetime

    data = draft_data
    native_commitment(
        data,
        individual_start_time=datetime(2026, 10, 5, 23, 30, tzinfo=UTC),
        individual_end_time=datetime(2026, 10, 6, 1, tzinfo=UTC),
    )
    shift_id = data.period.responses.get(user=data.member).offered_shifts.order_by("pk").last().pk
    result = validate_draft(
        data.coordinator, data.period.pk, **payload(data, [[data.member.pk, shift_id]])
    )
    assert not result["violations"]
    assert result["counts"][data.member.pk]["existing"] == 1
