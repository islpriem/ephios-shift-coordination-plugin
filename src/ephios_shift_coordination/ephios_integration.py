from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.translation import gettext as _
from ephios.core.dynamic_preferences_registry import GeneralRequiredQualificationPreference
from ephios.core.models import Event, Qualification
from ephios.core.services.qualification import collect_all_included_qualifications
from guardian.shortcuts import get_objects_for_user

from .models import PlannedShift
from .services import Conflict


def observer_name(name):
    """Native display name of a member sitting in without working hours."""
    return _("{name} · sitting in").format(name=name)


def type_requirements(event_type):
    return sorted(q.pk for q in event_type.preferences[GeneralRequiredQualificationPreference.name])


def eligible(user, planned_shift):
    shift = planned_shift.shift
    if not user.is_active or not shift or not shift.event.active:
        return False
    if not user.has_perm("core.view_event", shift.event):
        return False
    required = set(type_requirements(shift.event.type)) | set(
        shift.structure_configuration.get("required_qualification_ids", [])
    )
    valid_ids = [
        grant.qualification_id
        for grant in user.qualification_grants.all()
        if grant.expires is None or grant.expires >= max(timezone.now(), shift.end_time)
    ]
    qualifications = collect_all_included_qualifications(
        Qualification.objects.filter(pk__in=valid_ids)
    )
    return required <= set(qualifications.values_list("pk", flat=True))


def eligibility(users, planned_shifts):
    """Apply the rules of `eligible` to a whole cohort with few queries.

    Returns the visible native event IDs and the eligible planned shifts per user ID.
    """
    now = timezone.now()
    targets = Event.objects.filter(
        pk__in={planned.shift.event_id for planned in planned_shifts if planned.shift}
    )
    requirements, included, result = {}, {}, {}
    for user in users:
        visible = (
            set(
                get_objects_for_user(user, "core.view_event", klass=targets).values_list(
                    "pk", flat=True
                )
            )
            if user.is_active
            else set()
        )
        grants = list(user.qualification_grants.all())
        offered = []
        for planned in planned_shifts:
            shift = planned.shift
            if not shift or not shift.event.active or shift.event_id not in visible:
                continue
            if shift.event.type_id not in requirements:
                requirements[shift.event.type_id] = set(type_requirements(shift.event.type))
            required = requirements[shift.event.type_id] | set(
                shift.structure_configuration.get("required_qualification_ids", [])
            )
            valid = tuple(
                sorted(
                    grant.qualification_id
                    for grant in grants
                    if grant.expires is None or grant.expires >= max(now, shift.end_time)
                )
            )
            if valid not in included:
                included[valid] = set(
                    collect_all_included_qualifications(
                        Qualification.objects.filter(pk__in=valid)
                    ).values_list("pk", flat=True)
                )
            if required <= included[valid]:
                offered.append(planned)
        result[user.pk] = (visible, offered)
    return result


def period_shifts(period):
    return list(
        PlannedShift.objects.filter(event__period=period)
        .select_related("shift__event__type", "event")
        .order_by("event__date", "pk")
    )


def check_structure(period, user=None):
    shifts = period_shifts(period)
    for link in period.events.select_related("event"):
        event = link.event
        if (
            event is None
            or not event.active
            or event.type_id != period.template_snapshot["event_type"]
        ):
            raise Conflict(_("The original event was deleted or changed. Check the native events."))
        if user and not (
            user.has_perm("core.view_event", event) and user.has_perm("core.change_event", event)
        ):
            raise PermissionDenied
        if set(event.shifts.values_list("pk", flat=True)) != set(
            link.shifts.values_list("original_shift_id", flat=True)
        ):
            raise Conflict(_("The event's shifts have changed. Check the native events."))
    requirements = {}
    for planned in shifts:
        shift, snapshot = planned.shift, planned.snapshot
        if (
            shift is None
            or shift.event_id != planned.event.event_id
            or shift.signup_flow_slug != "manual"
            or shift.structure_slug != "uniform"
            or shift.label != snapshot["label"]
            or shift.structure_configuration != snapshot["structure_configuration"]
            or any(
                getattr(shift, field) != datetime.fromisoformat(snapshot[field])
                for field in ("meeting_time", "start_time", "end_time")
            )
            or shift.participations.exists()
        ):
            raise Conflict(
                _("A shift was changed or already has participations. Check the native events.")
            )
        requirements[str(shift.event.type_id)] = type_requirements(shift.event.type)
    if period.opened_at and requirements != period.opened_structure:
        raise Conflict(_("Event type qualifications have changed since the survey opened."))
    return shifts, requirements
