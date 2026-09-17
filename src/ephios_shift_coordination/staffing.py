"""Replacement overview and self-service staffing of published shifts."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _
from ephios.core.models import AbstractParticipation, LocalParticipation, Shift, UserProfile
from ephios.core.models.events import PlaceholderParticipation
from ephios.core.models.users import Notification
from ephios.core.services.notifications.backends import send_all_notifications
from guardian.shortcuts import get_users_with_perms

from . import scheduling as rules
from .access import can_plan, enabled
from .drafts import RULE_LABELS
from .ephios_integration import eligible, observer_name
from .models import (
    Availability,
    ObserverParticipation,
    PlannedEvent,
    PlannedShift,
    PlanningPeriod,
)
from .services import Conflict

CONFIRMED = AbstractParticipation.States.CONFIRMED
# Rules that decide the role instead of warning about an otherwise possible assignment.
# "regular_minimum" and "shift_incomplete" belong to the shift as a whole and are shown
# through its staffing badge, not as an objection against a single candidate.
ROLE_CODES = {
    "qualification",
    "missing_response",
    "observer_wish",
    "unavailable",
    "regular_minimum",
    "shift_incomplete",
}
# Best answers first; anything unexpected sorts last instead of breaking the page.
RATING_ORDER = {"preferred": 0, "available": 1, "if_needed": 2, None: 3}


def members(planned_shift):
    """Confirmed participations of a shift together with the member behind them."""
    observers = {
        record.participation_id: record.user
        for record in planned_shift.observers.select_related("user")
    }
    rows = []
    for participation in planned_shift.shift.participations.all():
        if participation.state != CONFIRMED:
            continue
        user = observers.get(participation.pk) or getattr(participation, "user", None)
        rows.append(
            {
                "participation": participation,
                "user": user,
                "observer": participation.pk in observers,
                "name": str(user) if user else str(participation.participant),
            }
        )
    return sorted(rows, key=lambda row: (row["observer"], row["name"]))


def staffing(planned_shift):
    """Staffing as the plugin counts it: people sitting in staff a shift, but never alone."""
    rows = members(planned_shift)
    configuration = planned_shift.shift.structure_configuration
    minimum = configuration["minimum_number_of_participants"]
    maximum = configuration["maximum_number_of_participants"]
    regular = [row for row in rows if not row["observer"]]
    required = min(planned_shift.event.period.rules.get("minimum_regular", 1), minimum)
    return {
        "shift": planned_shift,
        "rows": rows,
        "regular": regular,
        "observers": [row for row in rows if row["observer"]],
        "count": len(rows),
        "minimum": minimum,
        "maximum": maximum,
        "full": maximum is not None and len(rows) >= maximum,
        "sufficient": len(rows) >= minimum and len(regular) >= required,
    }


def conflicting(user, planned_shift):
    """True when the member is already confirmed elsewhere at that time, in any role."""
    shift = planned_shift.shift
    overlapping = Q(start_time__lt=shift.end_time, end_time__gt=shift.start_time)
    if (
        LocalParticipation.objects.filter(
            user=user, state=CONFIRMED, shift__event__type_id=shift.event.type_id
        )
        .exclude(shift_id=shift.pk)
        .filter(overlapping)
        .exists()
    ):
        return True
    return (
        ObserverParticipation.objects.filter(user=user, participation__state=CONFIRMED)
        .exclude(planned_shift=planned_shift)
        .filter(
            planned_shift__shift__start_time__lt=shift.end_time,
            planned_shift__shift__end_time__gt=shift.start_time,
        )
        .exists()
    )


def options(user, planned_shift):
    """What the viewer may do with this shift themselves."""
    state = staffing(planned_shift)
    period = planned_shift.event.period
    mine = next((row for row in state["rows"] if row["user"] == user), None)
    upcoming = planned_shift.shift.start_time > timezone.now()
    free = upcoming and not state["full"] and mine is None and not conflicting(user, planned_shift)
    return {
        **state,
        "mine": mine,
        "can_leave": bool(mine) and upcoming,
        "can_join": free and eligible(user, planned_shift),
        "can_observe": (
            free
            and bool(period.rules.get("allow_observers"))
            and user.is_active
            and user.has_perm("core.view_event", planned_shift.shift.event)
        ),
    }


def service_shifts(planned_event):
    return [
        planned
        for planned in planned_event.shifts.select_related(
            "shift__event__type", "event__period"
        ).order_by("pk")
        if planned.shift
    ]


def replacement_access(user, planned_event):
    """Coordinators and everybody staffed that day may use the replacement page."""
    event = planned_event.event
    if (
        not enabled()
        or not user.is_active
        or event is None
        or not user.has_perm("core.view_event", event)
    ):
        raise PermissionDenied
    if planned_event.period.state != PlanningPeriod.State.PUBLISHED:
        raise Conflict(_("This service plan has not been published yet."))
    coordinator = can_plan(user) and user.has_perm("core.change_event", event)
    if not coordinator and not any(
        row["user"] == user for planned in service_shifts(planned_event) for row in members(planned)
    ):
        raise PermissionDenied
    return coordinator


def day_input(planned_event, shifts, people):
    """Pure rule input for one service day and the members who could step in."""
    period = planned_event.period
    zone = ZoneInfo(period.timezone)
    responses = {
        response.user_id: response
        for response in period.responses.filter(user__in=people).prefetch_related(
            "availabilities", "offered_shifts", "observer_events"
        )
    }
    entries, availability, observers = [], [], []
    for user in people:
        response = responses.get(user.pk)
        ratings = {a.planned_shift_id: a.rating for a in response.availabilities.all()}
        offered = {shift.pk for shift in response.offered_shifts.all()}
        complete = bool(response.submitted_at and response.maximum is not None)
        entries.append(rules.Person(user.pk, response.maximum if complete else None, complete))
        wished = {link.pk for link in response.observer_events.all()}
        for planned in shifts:
            availability.append(
                rules.Availability(
                    user.pk,
                    planned.pk,
                    ratings.get(planned.pk) if planned.pk in offered else None,
                    eligible(user, planned),
                )
            )
            if planned_event.pk in wished and period.rules.get("allow_observers"):
                observers.append(rules.Observer(user.pk, planned.pk))
    pure_shifts = tuple(
        rules.Shift(
            planned.pk,
            planned.shift_id,
            planned.shift.event.type_id,
            planned.shift.start_time.astimezone(UTC),
            planned.shift.end_time.astimezone(UTC),
            planned.shift.start_time.astimezone(zone).date(),
            planned.shift.structure_configuration["minimum_number_of_participants"],
            planned.shift.structure_configuration["maximum_number_of_participants"],
        )
        for planned in shifts
    )
    first = min(rules.week_start(period.start_date), period.start_date - timedelta(days=1))
    last = max(
        rules.week_start(period.end_date) + timedelta(days=7), period.end_date + timedelta(days=2)
    )
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
            user__in=people,
            state=CONFIRMED,
            shift__event__type_id=period.template_snapshot["event_type"],
        )
        .filter(
            Q(
                start_time__gte=datetime.combine(first, time.min, zone),
                start_time__lt=datetime.combine(last, time.min, zone),
            )
            | Q(
                start_time__lt=max(s.end for s in pure_shifts),
                end_time__gt=min(s.start for s in pure_shifts),
            )
        )
        .exclude(shift_id__in=[planned.shift_id for planned in shifts])
        .values("pk", "user_id", "shift__event__type_id", "start_time", "end_time")
    )
    return rules.PlanningInput(
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
            },
            minimum_regular=period.rules.get("minimum_regular", 1),
        ),
        pure_shifts,
        tuple(entries),
        tuple(availability),
        commitments,
        observers=tuple(observers),
    )


def replacements(planned_event, coordinator):
    """People who answered positively for this day, with role and rule warnings per shift."""
    period = planned_event.period
    shifts = service_shifts(planned_event)
    if not shifts:
        return []
    positive = {"if_needed", "available", "preferred"}
    candidates = []
    for response in period.responses.select_related("user").prefetch_related(
        "availabilities", "observer_events"
    ):
        if not response.submitted_at or not response.user.is_active:
            continue
        ratings = {a.planned_shift_id: a.rating for a in response.availabilities.all()}
        wished = {link.pk for link in response.observer_events.all()}
        if any(ratings.get(planned.pk) in positive for planned in shifts) or (
            planned_event.pk in wished and period.rules.get("allow_observers")
        ):
            candidates.append(response.user)
    if not candidates:
        return []
    data = day_input(planned_event, shifts, candidates)
    wishes = {(o.person_id, o.shift_id) for o in data.observers}
    ratings = {(a.person_id, a.shift_id): a for a in data.availability}
    result = []
    for planned in shifts:
        state = staffing(planned)
        taken = {row["user"] for row in state["rows"] if row["user"]}
        rows = []
        for user in candidates:
            if user in taken:
                continue
            answer = ratings[user.pk, planned.pk]
            observing = (user.pk, planned.pk) in wishes
            # Somebody who offered to sit in on the service may still have ruled out this
            # very shift; that answer counts and keeps them out of the list.
            if answer.rating == "unavailable":
                continue
            if answer.rating not in positive and not observing:
                continue
            regular = answer.eligible and answer.rating in positive
            if not regular and not observing:
                continue
            checks = rules.validate(
                data,
                [(user.pk, planned.pk)] if regular else [],
                [] if regular else [(user.pk, planned.pk)],
            )
            codes = [v.code for v in checks.violations if v.code not in ROLE_CODES]
            rows.append(
                {
                    "user": user,
                    "name": str(user),
                    "rating": answer.rating,
                    "rating_label": str(Availability.Rating(answer.rating).label)
                    if answer.rating
                    else "",
                    "regular": regular,
                    "observer": observing and not regular,
                    "blocked": "overlap" in codes,
                    "warnings": [str(RULE_LABELS[code]) for code in dict.fromkeys(codes)],
                }
            )
        rows.sort(
            key=lambda row: (
                row["blocked"],
                not row["regular"],
                RATING_ORDER.get(row["rating"], len(RATING_ORDER)),
                bool(row["warnings"]),
                row["name"],
            )
        )
        result.append({"planned": planned, "staffing": state, "candidates": rows})
    return result


def locked(actor, planned_shift_id):
    planned = get_object_or_404(
        PlannedShift.objects.select_related("shift__event__type", "event__period"),
        pk=planned_shift_id,
    )
    if not enabled() or not actor.is_active or planned.shift is None:
        raise PermissionDenied
    PlanningPeriod.objects.select_for_update().get(pk=planned.event.period_id)
    Shift.objects.select_for_update().get(pk=planned.shift_id)
    planned.refresh_from_db()
    if planned.event.period.state != PlanningPeriod.State.PUBLISHED:
        raise Conflict(_("This service plan has not been published yet."))
    if not planned.shift.event.active or planned.shift.start_time <= timezone.now():
        raise Conflict(_("This shift has already started. Please contact the coordinators."))
    return planned


def check_actor(actor, planned, target):
    event = planned.shift.event
    if target is None or not target.is_active or not target.has_perm("core.view_event", event):
        raise ValidationError(_("This person cannot be assigned to that service."))
    if actor == target:
        return False
    if not can_plan(actor) or not actor.has_perm("core.change_event", event):
        raise PermissionDenied
    return True


@transaction.atomic
def add_person(actor, planned_shift_id, *, user_id, observer=False):
    """Staff a published shift, either by yourself or through a coordinator."""
    planned = locked(actor, planned_shift_id)
    target = UserProfile.objects.select_for_update().filter(pk=user_id).first()
    by_coordinator = check_actor(actor, planned, target)
    state = staffing(planned)
    if any(row["user"] == target for row in state["rows"]):
        raise Conflict(_("This person is already staffed for that shift."))
    if state["full"]:
        raise ValidationError(_("This shift is already fully staffed."))
    if conflicting(target, planned):
        raise ValidationError(_("This person is already assigned to another service at that time."))
    if observer:
        if not planned.event.period.rules.get("allow_observers"):
            raise ValidationError(_("Sitting in is switched off for this period."))
        record = ObserverParticipation.objects.filter(planned_shift=planned, user=target).first()
        if record:
            participation = record.participation
            participation.state = CONFIRMED
            participation.save()
        else:
            participation = PlaceholderParticipation(
                shift=planned.shift,
                display_name=observer_name(str(target)),
                state=CONFIRMED,
            )
            participation.save()
            ObserverParticipation.objects.create(
                participation=participation, planned_shift=planned, user=target
            )
    else:
        if not eligible(target, planned):
            raise ValidationError(
                _("This person does not have the qualifications required for that shift.")
            )
        participation = LocalParticipation.objects.filter(
            shift=planned.shift, user=target
        ).first() or LocalParticipation(shift=planned.shift, user=target)
        participation.state = CONFIRMED
        participation.save()
    if by_coordinator:
        notify(target, planned, "assigned", observer=observer, actor=str(actor))
    transaction.on_commit(lambda: send_all_notifications(), robust=True)
    return planned


@transaction.atomic
def remove_person(actor, planned_shift_id, *, user_id):
    """Sign off from a published shift and warn the coordinators about a gap."""
    planned = locked(actor, planned_shift_id)
    target = UserProfile.objects.select_for_update().filter(pk=user_id).first()
    by_coordinator = check_actor(actor, planned, target)
    before = staffing(planned)
    row = next((entry for entry in before["rows"] if entry["user"] == target), None)
    if row is None:
        raise Conflict(_("This person is not staffed for that shift any more."))
    participation = row["participation"]
    participation.state = (
        AbstractParticipation.States.RESPONSIBLE_REJECTED
        if by_coordinator
        else AbstractParticipation.States.USER_DECLINED
    )
    participation.save()
    if by_coordinator:
        notify(target, planned, "removed", observer=row["observer"], actor=str(actor))
    if before["sufficient"] and not staffing(planned)["sufficient"]:
        warn_coordinators(actor, planned, str(target))
    transaction.on_commit(lambda: send_all_notifications(), robust=True)
    return planned


def notify(target, planned, change, *, observer, actor):
    Notification.objects.create(
        user=target,
        slug="shift_coordination_staffing",
        data={
            "period_id": planned.event.period_id,
            "planned_shift_id": planned.pk,
            "change": change,
            "observer": observer,
            "actor": actor,
        },
    )


def warn_coordinators(actor, planned, person):
    recipients = [
        user
        for user in get_users_with_perms(
            planned.shift.event, only_with_perms_in=["change_event"]
        ).filter(is_active=True)
        if can_plan(user) and user != actor
    ]
    Notification.objects.bulk_create(
        [
            Notification(
                user=user,
                slug="shift_coordination_understaffed",
                data={
                    "period_id": planned.event.period_id,
                    "planned_shift_id": planned.pk,
                    "person": person,
                },
            )
            for user in recipients
        ]
    )


def service(planned_event_id):
    return get_object_or_404(
        PlannedEvent.objects.select_related("event", "period"), pk=planned_event_id
    )
