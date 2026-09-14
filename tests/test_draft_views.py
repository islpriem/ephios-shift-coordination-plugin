import pytest
from django.test import Client
from django.urls import reverse

from ephios_shift_coordination.drafts import save_draft, validate_draft
from tests.test_drafts import draft_data as draft_data
from tests.test_drafts import payload
from tests.test_surveys import survey_data as survey_data


def url(data, action="plan"):
    return reverse(f"ephios_shift_coordination:{action}", args=[data.period.pk])


def test_coordinator_calendar_and_json_are_private_and_escape_notes(client, draft_data):
    data = draft_data
    client.force_login(data.coordinator)
    page = client.get(url(data))
    assert page.status_code == 200
    assert b"planning-data" in page.content
    assert b"<script>private answer</script>" not in page.content
    check = client.post(url(data, "draft_validate"), payload(data), content_type="application/json")
    assert check.status_code == 200 and check.json()["counts"][str(data.member.pk)]["draft"] == 1
    saved = client.post(
        url(data, "draft_save"),
        {**payload(data), "confirmations": []},
        content_type="application/json",
    )
    assert saved.status_code == 200
    for role in (data.member, data.outsider):
        client.force_login(role)
        assert client.get(url(data)).status_code == 403
        assert (
            client.post(
                url(data, "draft_validate"), payload(data), content_type="application/json"
            ).status_code
            == 403
        )
        assert (
            client.post(url(data, "draft_save"), {}, content_type="application/json").status_code
            == 403
        )


def test_methods_csrf_bad_json_conflicts_and_missing_period(client, draft_data):
    data = draft_data
    client.force_login(data.coordinator)
    assert client.post(url(data)).status_code == 405
    for action in ("draft_validate", "draft_save"):
        target = url(data, action)
        assert client.get(target).status_code == 405
        for body in ("{", "[]", "{}", '{"assignments":null}'):
            assert client.post(target, body, content_type="application/json").status_code == 400
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(data.coordinator)
    assert (
        protected.post(url(data, "draft_save"), {}, content_type="application/json").status_code
        == 403
    )
    body = {**payload(data), "expected_version": 0}
    assert (
        client.post(url(data, "draft_validate"), body, content_type="application/json").status_code
        == 409
    )
    assert client.get(reverse("ephios_shift_coordination:plan", args=[99999])).status_code == 404
    data.period.state = data.period.State.PUBLISHED
    data.period.save()
    assert client.get(url(data)).status_code == 409


@pytest.mark.parametrize("action", ["draft_validate", "draft_save"])
def test_invalid_selection_is_400(client, draft_data, action):
    client.force_login(draft_data.coordinator)
    body = {**payload(draft_data), "assignments": [[99999, draft_data.shifts[0].pk]]}
    if action == "draft_save":
        body["confirmations"] = []
    assert (
        client.post(url(draft_data, action), body, content_type="application/json").status_code
        == 400
    )


def test_recorded_rule_reason_and_people_are_visible_only_to_coordinators(client, draft_data):
    data = draft_data
    response = data.period.responses.get(user=data.member)
    response.maximum = 0
    response.save()
    initial = payload(data)
    result = validate_draft(data.coordinator, data.period.pk, **initial)
    save_draft(
        data.coordinator,
        data.period.pk,
        **initial,
        confirmations=[
            {"token": v["token"], "confirmed": True, "reason": "<script>reason</script>"}
            for v in result["violations"]
        ],
    )
    client.force_login(data.coordinator)
    page = client.get(url(data))
    assert page.status_code == 200
    assert b"Personal maximum exceeded" in page.content
    assert b"&lt;script&gt;reason&lt;/script&gt;" in page.content
    detail = client.get(data.period.get_absolute_url())
    assert url(data).encode() in detail.content
