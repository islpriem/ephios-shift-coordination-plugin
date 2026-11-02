"""One real planning cycle for simultaneous phone and chat shifts."""

from datetime import time

from django.urls import reverse
from ephios.core.models import LocalParticipation, QualificationGrant

from ephios_shift_coordination.drafts import load_plan, save_draft, validate_draft
from ephios_shift_coordination.models import ObserverParticipation, RuleOverride
from ephios_shift_coordination.proposals import create_proposal
from ephios_shift_coordination.publication import publish_plan, review_publication
from ephios_shift_coordination.services import create_period
from ephios_shift_coordination.staffing import staffing
from ephios_shift_coordination.surveys import open_survey, save_response
from tests.test_periods import request_data


def test_parallel_proposal_saves_and_publishes_without_overlap_exceptions(
    planning_data, monkeypatch, client
):
    data = planning_data
    for shift in data.template.shifts.all():
        shift.start_time = time(18)
        shift.meeting_time = time(17, 45)
        shift.end_time = time(22)
        shift.minimum = shift.maximum = 2
        shift.save()
        for person in (data.member, data.coordinator):
            QualificationGrant.objects.create(
                user=person, qualification=shift.qualifications.first()
            )
    data.configuration.allow_observers = True
    data.configuration.save()
    args = request_data(data)
    args["dates"] = args["dates"][:1]
    period = create_period(data.coordinator, **args)
    period = open_survey(data.coordinator, period.pk, expected_version=1)
    shifts = list(period.events.first().shifts.order_by("pk"))
    for person in (data.member, data.coordinator):
        save_response(
            person,
            period.pk,
            expected_version=0,
            maximum=1,
            notes="",
            ratings={s.pk: "preferred" if s == shifts[0] else "available" for s in shifts},
            observer_events=[],
        )
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline)
    state = load_plan(data.coordinator, period.pk)
    basis = {"expected_version": state["version"], "fingerprint": state["fingerprint"]}
    proposal = create_proposal(data.coordinator, period.pk, **basis)
    assert proposal["score"]["filled"] == 2
    body = {
        **basis,
        "assignments": [list(p) for p in proposal["assignments"]],
        "observers": [list(p) for p in proposal["observers"]],
    }
    assert not validate_draft(data.coordinator, period.pk, **body)["violations"]
    saved = save_draft(data.coordinator, period.pk, **body, confirmations=[])
    review = review_publication(data.coordinator, period.pk)
    assert review["filled"] == 2 and not review["overrides"]
    published = publish_plan(
        data.coordinator,
        period.pk,
        expected_version=saved["version"],
        fingerprint=saved["fingerprint"],
        confirmed_tokens=[],
        confirm_underfilled=False,
        confirm_publish=True,
    )
    assert not RuleOverride.objects.filter(period=period).exists()
    assert LocalParticipation.objects.count() == 2
    assert ObserverParticipation.objects.count() == 2
    assert all(staffing(s)["sufficient"] for s in shifts)
    snapshot = published.publication_snapshot
    # Real endpoints let a member give back and retake the concurrent observer role.
    observer = ObserverParticipation.objects.get(user=data.member)
    client.force_login(data.member)
    endpoint = reverse(
        "ephios_shift_coordination:staffing_action", args=[observer.planned_shift_id]
    )
    for action in ("leave", "observe"):
        response = client.post(endpoint, {"action": action, "user_id": data.member.pk}, follow=True)
        assert response.status_code == 200
    observer.participation.refresh_from_db()
    assert observer.participation.state == LocalParticipation.States.CONFIRMED
    published.refresh_from_db()
    assert published.publication_snapshot == snapshot
