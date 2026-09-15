from dataclasses import asdict
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import Client
from django.urls import reverse
from ephios.core.models import LocalParticipation
from ephios.core.models.users import Notification

from ephios_shift_coordination import proposals
from ephios_shift_coordination.models import DraftAssignment
from ephios_shift_coordination.services import Conflict
from tests.test_drafts import draft_data as draft_data
from tests.test_drafts import payload
from tests.test_surveys import survey_data as survey_data


def basis(data):
    initial = payload(data)
    return {key: initial[key] for key in ("expected_version", "fingerprint")}


def url(data):
    return reverse("ephios_shift_coordination:plan_propose", args=[data.period.pk])


def test_real_proposal_is_private_data_free_and_has_no_persistent_effect(draft_data, monkeypatch):
    data = draft_data
    original = proposals.propose_plan

    def inspect(pure_input):
        assert "private answer" not in str(asdict(pure_input))
        assert "member" not in str(asdict(pure_input))
        return original(pure_input)

    monkeypatch.setattr(proposals, "propose_plan", inspect)
    notifications = Notification.objects.count()
    initial = basis(data)
    result = proposals.create_proposal(data.coordinator, data.period.pk, **initial)
    assert result["status"] == "optimal_primary" and result["assignments"]
    assert result["version"] == initial["expected_version"]
    assert result["fingerprint"] == initial["fingerprint"]
    assert not DraftAssignment.objects.exists() and not LocalParticipation.objects.exists()
    assert Notification.objects.count() == notifications
    data.period.refresh_from_db()
    assert data.period.version == initial["expected_version"]
    assert data.period.state == data.period.State.SURVEY_OPEN


@pytest.mark.parametrize("change", ["version", "grant", "role", "inactive", "state"])
def test_changes_during_calculation_reject_the_proposal(draft_data, monkeypatch, change):
    from guardian.shortcuts import remove_perm

    data = draft_data
    original = proposals.propose_plan

    def calculate(pure_input):
        result = original(pure_input)
        if change == "version":
            data.period.version += 1
            data.period.save()
        elif change == "state":
            data.period.state = data.period.State.PUBLISHED
            data.period.save()
        elif change == "grant":
            grant = data.member.qualification_grants.get()
            grant.expires = data.shifts[0].shift.end_time + timedelta(days=100)
            grant.save()
        elif change == "inactive":
            data.coordinator.is_active = False
            data.coordinator.save()
        else:
            remove_perm("ephios_shift_coordination.manage_planning", data.coordination)
        return result

    monkeypatch.setattr(proposals, "propose_plan", calculate)
    with pytest.raises((Conflict, PermissionDenied)):
        proposals.create_proposal(data.coordinator, data.period.pk, **basis(data))
    assert not DraftAssignment.objects.exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_calculation_runs_outside_database_transaction(draft_data, monkeypatch):
    original = proposals.propose_plan

    def inspect(pure_input):
        assert not connection.in_atomic_block
        return original(pure_input)

    monkeypatch.setattr(proposals, "propose_plan", inspect)
    proposals.create_proposal(draft_data.coordinator, draft_data.period.pk, **basis(draft_data))


def test_endpoint_requires_fresh_version_access_csrf_and_exact_request(client, draft_data):
    data = draft_data
    client.force_login(data.coordinator)
    body = basis(data)
    response = client.post(url(data), body, content_type="application/json")
    assert response.status_code == 200 and response.json()["score"]["filled"] == 2
    assert client.get(url(data)).status_code == 405
    for malformed in ("{", "[]", "{}"):
        assert client.post(url(data), malformed, content_type="application/json").status_code == 400
    assert (
        client.post(
            url(data), {**body, "assignments": []}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            url(data), {**body, "expected_version": 0}, content_type="application/json"
        ).status_code
        == 409
    )
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(data.coordinator)
    assert csrf.post(url(data), body, content_type="application/json").status_code == 403
    client.force_login(data.member)
    assert client.post(url(data), body, content_type="application/json").status_code == 403
