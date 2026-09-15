"""Current native inputs, protected rule checks and atomic shared draft saves."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from ephios.core.models import (
    Event,
    LocalParticipation,
    Qualification,
    QualificationGrant,
    Shift,
    UserProfile,
)
from ephios.core.services.qualification import collect_all_included_qualifications
from guardian.shortcuts import get_objects_for_user

from . import scheduling as rules
from .access import can_plan, enabled
from .ephios_integration import check_structure
from .models import Availability, DraftAssignment, PlanningPeriod, RuleOverride
from .services import Conflict

RULE_LABELS = {
    "missing_response": gettext_lazy("No complete response for this shift"),
    "unavailable": gettext_lazy("Marked unavailable"),
    "qualification": gettext_lazy("Required qualification missing or expired"),
    "personal_maximum": gettext_lazy("Personal maximum exceeded"),
    "weekly_limit": gettext_lazy("Weekly limit exceeded"),
    "consecutive_days": gettext_lazy("Services on consecutive days"),
    "overlap": gettext_lazy("Overlapping services of the same event type"),
    "shift_maximum": gettext_lazy("Shift maximum exceeded"),
}


@dataclass
class Snapshot:
    period: PlanningPeriod
    data: rules.PlanningInput
    fingerprint: str
    allowed: set
    people: list
    shifts: list


def snapshot(user, period_id, *, lock=False):
    # The period lock serializes all plugin writes, including deadline transitions.
    period = get_object_or_404(PlanningPeriod.objects.select_for_update(), pk=period_id)
    user = UserProfile.objects.filter(pk=user.pk).first()
    if not user or not enabled() or not can_plan(user):
        raise PermissionDenied
    if period.effective_state != PlanningPeriod.State.PLANNING:
        raise Conflict(
            _("Planning is available only after the response deadline and before publication.")
        )
    users = UserProfile.objects.filter(is_active=True).order_by("pk")
    if lock:
        users = users.select_for_update()
    grants_query = QualificationGrant.objects.order_by("pk")
    if lock:
        grants_query = grants_query.select_for_update()
    users = list(users.prefetch_related(Prefetch("qualification_grants", queryset=grants_query)))
    if lock:
        list(
            Event.all_objects.filter(planning_link__period=period)
            .order_by("pk")
            .select_for_update()
        )
        list(
            Shift.objects.filter(plannedshift__event__period=period)
            .order_by("pk")
            .select_for_update()
        )
        # Native disposition can update an existing participation without locking its user.
        # Lock all states and dates: an edit may move one into the planning window.
        list(
            LocalParticipation.objects.filter(
                user_id__in=[person.pk for person in users],
                shift__event__type_id=period.template_snapshot["event_type"],
            )
            .order_by("pk")
            .select_for_update()
        )
        # Permissions may have changed while waiting for a native writer's locks.
        user = UserProfile.objects.filter(pk=user.pk).first()
        if not user or not enabled() or not can_plan(user):
            raise PermissionDenied
    planned, requirements = check_structure(period, user)
    zone = ZoneInfo(period.timezone)
    targets = Event.objects.filter(pk__in=[link.event.event_id for link in planned])
    responses = {
        r.user_id: r for r in period.responses.prefetch_related("availabilities", "offered_shifts")
    }
    people, presentation, availability, grants, allowed = [], [], [], [], set()
    included_cache = {}
    for person in users:
        visible = set(
            get_objects_for_user(person, "core.view_event", klass=targets).values_list(
                "pk", flat=True
            )
        )
        if not visible:
            continue
        response = responses.get(person.pk)
        ratings = (
            {a.planned_shift_id: a.rating for a in response.availabilities.all()}
            if response
            else {}
        )
        offered = {s.pk for s in response.offered_shifts.all()} if response else set()
        complete = bool(
            response
            and response.submitted_at
            and response.maximum is not None
            and offered == set(ratings)
        )
        people.append(rules.Person(person.pk, response.maximum if complete else None, complete))
        presentation.append(
            {
                "id": person.pk,
                "name": str(person),
                "complete": complete,
                "maximum": response.maximum if complete else None,
                "notes": response.notes if complete else "",
            }
        )
        person_grants = sorted(person.qualification_grants.all(), key=lambda grant: grant.pk)
        grants.append(
            (
                person.pk,
                [(g.pk, g.qualification_id, g.expires) for g in person_grants],
                response.version if response else None,
            )
        )
        for link in planned:
            native = link.shift
            if native.event_id not in visible:
                continue
            allowed.add((person.pk, link.pk))
            valid = tuple(
                sorted(
                    g.qualification_id
                    for g in person_grants
                    if g.expires is None or g.expires >= max(timezone.now(), native.end_time)
                )
            )
            if valid not in included_cache:
                included_cache[valid] = set(
                    collect_all_included_qualifications(
                        Qualification.objects.filter(pk__in=valid)
                    ).values_list("pk", flat=True)
                )
            required = set(requirements[str(native.event.type_id)]) | set(
                native.structure_configuration.get("required_qualification_ids", [])
            )
            availability.append(
                rules.Availability(
                    person.pk, link.pk, ratings.get(link.pk), required <= included_cache[valid]
                )
            )
    shifts = tuple(
        rules.Shift(
            link.pk,
            link.shift_id,
            link.shift.event.type_id,
            link.shift.start_time.astimezone(UTC),
            link.shift.end_time.astimezone(UTC),
            link.shift.start_time.astimezone(zone).date(),
            link.shift.structure_configuration["minimum_number_of_participants"],
            link.shift.structure_configuration["maximum_number_of_participants"],
        )
        for link in planned
    )
    # Start dates cover interval totals, complete weeks and neighboring days.
    # The overlap alternative also captures arbitrarily long earlier commitments.
    first = min(rules.week_start(period.start_date), period.start_date - timedelta(days=1))
    last = max(
        rules.week_start(period.end_date) + timedelta(days=7), period.end_date + timedelta(days=2)
    )
    lower = datetime.combine(first, time.min, zone)
    upper = datetime.combine(last, time.min, zone)
    commitments = tuple(
        rules.Commitment(
            row["pk"],
            row["user_id"],
            row["shift__event__type_id"],
            row["start_time"],
            row["end_time"],
            row["start_time"].astimezone(zone).date(),
        )
        for row in LocalParticipation.objects.filter(
            user_id__in=[p.id for p in people],
            state=LocalParticipation.States.CONFIRMED,
            shift__event__type_id=period.template_snapshot["event_type"],
        )
        .filter(
            Q(start_time__gte=lower, start_time__lt=upper)
            | Q(
                start_time__lt=max(s.end for s in shifts), end_time__gt=min(s.start for s in shifts)
            )
        )
        .order_by("pk")
        .values("pk", "user_id", "shift__event__type_id", "start_time", "end_time")
    )
    data = rules.PlanningInput(
        rules.Period(
            period.pk,
            period.start_date,
            period.end_date,
            period.timezone,
            period.template_snapshot["event_type"],
        ),
        rules.Rules(
            **{
                key: period.rules[key]
                for key in ("weekly_limit", "free_next_day", "solver_seconds")
            }
        ),
        shifts,
        tuple(people),
        tuple(availability),
        commitments,
    )
    fingerprint = rules.fingerprint(
        {"data": asdict(data), "grants": grants, "allowed": sorted(allowed)}
    )
    return Snapshot(
        period,
        data,
        fingerprint,
        allowed,
        presentation,
        [
            {
                "id": link.pk,
                "label": link.shift.label,
                "start": link.shift.start_time.astimezone(zone).isoformat(),
                "end": link.shift.end_time.astimezone(zone).isoformat(),
                "date": shift.local_date.isoformat(),
                "minimum": shift.minimum,
                "maximum": shift.maximum,
            }
            for link, shift in zip(planned, shifts, strict=True)
        ],
    )


def public_violation(violation):
    # Never return native event titles, IDs or times from validation facts.
    return {
        "code": violation.code,
        "message": str(RULE_LABELS[violation.code]),
        "token": violation.token,
        "person_ids": violation.person_ids,
        "shift_ids": violation.shift_ids,
    }


def public_result(result):
    return {
        "violations": [public_violation(v) for v in result.violations],
        "existing_violations": [public_violation(v) for v in result.existing_violations],
        "underfilled": result.underfilled,
        "counts": result.counts,
    }


@transaction.atomic
def load_plan(user, period_id):
    current = snapshot(user, period_id)
    assignments = list(
        current.period.draft_assignments.order_by(
            "original_user_id", "planned_shift_id"
        ).values_list("original_user_id", "planned_shift_id")
    )
    valid = [pair for pair in assignments if pair in current.allowed]
    result = rules.validate(current.data, valid)
    return {
        "version": current.period.version,
        "fingerprint": current.fingerprint,
        "assignments": [list(pair) for pair in assignments],
        "invalid_assignments": [list(pair) for pair in assignments if pair not in current.allowed],
        "people": current.people,
        "shifts": current.shifts,
        "availability": [asdict(a) for a in current.data.availability],
        "ratings": {key: str(label) for key, label in Availability.Rating.choices},
        **public_result(result),
    }


def check_basis(current, expected_version, fingerprint):
    if (
        type(expected_version) is not int
        or expected_version != current.period.version
        or fingerprint != current.fingerprint
    ):
        raise Conflict(_("The draft or its inputs have changed. Reload before continuing."))


def checked(current, expected_version, fingerprint, assignments):
    check_basis(current, expected_version, fingerprint)
    if not isinstance(assignments, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or any(type(value) is not int for value in pair)
        for pair in assignments
    ):
        raise ValidationError(_("Invalid assignment selection."))
    pairs = [tuple(pair) for pair in assignments]
    if any(pair not in current.allowed for pair in pairs):
        raise ValidationError(
            _(
                "A selected person or shift is unavailable or lacks event access. "
                "Remove that assignment."
            )
        )
    try:
        return rules.validate(current.data, pairs)
    except ValueError as exc:
        raise ValidationError(_("Invalid assignment selection.")) from exc


@transaction.atomic
def validate_draft(user, period_id, *, expected_version, fingerprint, assignments):
    current = snapshot(user, period_id)
    return public_result(checked(current, expected_version, fingerprint, assignments))


@transaction.atomic
def save_draft(user, period_id, *, expected_version, fingerprint, assignments, confirmations):
    current = snapshot(user, period_id, lock=True)
    result = checked(current, expected_version, fingerprint, assignments)
    if not isinstance(confirmations, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("token"), str)
        or item.get("confirmed") is not True
        or not isinstance(item.get("reason"), str)
        or not 1 <= len(item["reason"].strip()) <= 2000
        for item in confirmations
    ):
        raise ValidationError(
            _("Explicitly confirm every exception and provide a reason of at most 2000 characters.")
        )
    by_token = {item["token"]: item["reason"].strip() for item in confirmations}
    if len(by_token) != len(confirmations) or set(by_token) != {v.token for v in result.violations}:
        raise ValidationError(
            _("Confirm every current rule violation with a reason before saving.")
        )
    period = current.period
    period.version += 1
    period.state = PlanningPeriod.State.PLANNING
    period.draft_fingerprint = current.fingerprint
    period.save(update_fields=["version", "state", "draft_fingerprint"])
    period.draft_assignments.all().delete()
    DraftAssignment.objects.bulk_create(
        [
            DraftAssignment(period=period, planned_shift_id=sid, user_id=uid, original_user_id=uid)
            for uid, sid in assignments
        ]
    )
    RuleOverride.objects.bulk_create(
        [
            RuleOverride(
                period=period,
                draft_version=period.version,
                code=v.code,
                token=v.token,
                facts=json.loads(json.dumps(asdict(v), cls=DjangoJSONEncoder)),
                reason=by_token[v.token],
                actor_id=user.pk,
            )
            for v in result.violations
        ]
    )
    return {"version": period.version, "fingerprint": current.fingerprint, **public_result(result)}
