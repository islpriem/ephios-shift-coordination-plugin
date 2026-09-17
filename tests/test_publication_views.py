import pytest
from django.test import Client
from django.urls import reverse
from ephios.core.models import LocalParticipation
from ephios.modellogging.models import LogEntry

from ephios_shift_coordination.models import PlanningPeriod
from tests.test_drafts import draft_data as draft_data
from tests.test_publication import prepare
from tests.test_surveys import survey_data as survey_data


def addresses(data):
    return {
        name: reverse(f"ephios_shift_coordination:{name}", args=[data.period.pk])
        for name in ("publication", "publish")
    }


def html_payload(body):
    return {
        **body,
        "confirm_underfilled": "on" if body["confirm_underfilled"] else "",
        "confirm_publish": "on",
    }


def test_review_confirmation_publication_record_and_native_logging(client, draft_data):
    data = draft_data
    body = prepare(data)
    urls = addresses(data)
    client.force_login(data.coordinator)
    response = client.get(urls["publication"])
    assert response.status_code == 200
    assert b"Private" not in response.content
    assert not LocalParticipation.objects.exists()
    assert client.get(urls["publish"]).status_code == 405
    assert client.post(urls["publication"]).status_code == 405
    response = client.post(urls["publish"], html_payload(body))
    assert response.status_code == 302 and response.url == urls["publication"]
    record = client.get(response.url)
    assert record.status_code == 200
    assert "Published service plan" in record.content.decode()
    assert b'name="confirm_publish"' not in record.content
    participation = LocalParticipation.objects.get()
    assert LogEntry.objects.filter(
        attached_to_object_id=participation.shift.event_id, user=data.coordinator
    ).exists()
    assert client.post(urls["publish"], html_payload(body)).status_code == 302
    assert LocalParticipation.objects.count() == 1
    assert (
        client.get(reverse("ephios_shift_coordination:plan", args=[data.period.pk])).status_code
        == 302
    )
    assert client.get(data.period.get_absolute_url()).status_code == 200
    client.force_login(data.member)
    assert (
        client.get(data.period.responses.get(user=data.member).get_absolute_url()).status_code
        == 200
    )


def test_publish_rechecks_roles_csrf_stale_versions_and_rejects_alternate_assignments(
    client, draft_data
):
    data = draft_data
    body = html_payload(prepare(data))
    urls = addresses(data)
    for person in (data.member, data.outsider):
        client.force_login(person)
        assert client.get(urls["publication"]).status_code == 403
        assert client.post(urls["publish"], body).status_code == 403
    client.force_login(data.coordinator)
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(data.coordinator)
    assert csrf.post(urls["publish"], body).status_code == 403
    assert (
        client.post(
            urls["publish"], {**body, "expected_version": body["expected_version"] - 1}
        ).status_code
        == 409
    )
    assert client.post(urls["publish"], {**body, "assignments": "[]"}).status_code == 400
    assert client.post(urls["publish"], {**body, "confirm_publish": ""}).status_code == 400
    assert client.post(urls["publish"], {**body, "fingerprint": "bad"}).status_code == 400
    data.period.refresh_from_db()
    assert data.period.state == PlanningPeriod.State.PLANNING
    assert not LocalParticipation.objects.exists()


def test_saved_exceptions_are_escaped_and_explicitly_reconfirmed(client, draft_data):
    data = draft_data
    data.period.responses.filter(user=data.member).update(maximum=0)
    body = html_payload(prepare(data))
    data.period.rule_overrides.update(reason="<script>unsafe reason</script>")
    client.force_login(data.coordinator)
    urls = addresses(data)
    response = client.get(urls["publication"])
    assert b"&lt;script&gt;unsafe reason&lt;/script&gt;" in response.content
    assert b"<script>unsafe reason</script>" not in response.content
    choices = response.context["form"].fields["confirmed_tokens"].choices
    assert data.shifts[0].snapshot["label"] in choices[0][1]
    assert client.post(urls["publish"], {**body, "confirmed_tokens": []}).status_code == 400
    assert client.post(urls["publish"], body).status_code == 302
    assert client.get(urls["publication"]).status_code == 200


def test_review_conflicts_and_publication_input_types_are_rejected(client, draft_data):
    from django.core.exceptions import ValidationError

    from ephios_shift_coordination.publication import load_publication, publish_plan
    from ephios_shift_coordination.services import Conflict

    data = draft_data
    client.force_login(data.coordinator)
    urls = addresses(data)
    assert client.get(urls["publication"]).status_code == 409
    assert client.post(urls["publish"], {}).status_code == 409
    with pytest.raises(Conflict):
        load_publication(data.coordinator, data.period.pk)
    body = prepare(data)
    for change in (
        {"expected_version": True},
        {"confirmed_tokens": ["x", "x"]},
        {"confirmed_tokens": [1]},
        {"confirm_underfilled": 1},
        {"fingerprint": None},
    ):
        with pytest.raises(ValidationError):
            publish_plan(data.coordinator, data.period.pk, **{**body, **change})
    data.period.rule_overrides.create(
        draft_version=body["expected_version"],
        code="missing_response",
        token="bad",
        facts={},
        reason="Inconsistent record",
    )
    assert client.get(urls["publication"]).status_code == 409
