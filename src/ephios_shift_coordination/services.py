import hashlib
import json
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from ephios.core.forms.events import EventForm
from ephios.core.models import Shift, UserProfile
from ephios.core.signup.flow import enabled_signup_flows
from ephios.core.signup.structure import enabled_shift_structures
from guardian.shortcuts import get_objects_for_user

from .access import can_plan, enabled
from .dates import local_datetime
from .models import PlannedEvent, PlannedShift, PlanningPeriod, PlanningSettings, ServiceTemplate


class Conflict(Exception):
    pass


def preview_template(template, dates, timezone_name):
    template.full_clean()
    shifts = list(template.shifts.prefetch_related("qualifications"))
    if not shifts:
        raise ValidationError(_("A service template needs at least one shift."))
    result = []
    for day in dates:
        entries = []
        for shift in shifts:
            shift.full_clean()
            entries.append(
                {
                    "label": shift.label,
                    "meeting_time": local_datetime(day, shift.meeting_time, timezone_name),
                    "start_time": local_datetime(day, shift.start_time, timezone_name),
                    "end_time": local_datetime(
                        day + timedelta(days=shift.end_day_offset), shift.end_time, timezone_name
                    ),
                    "structure_configuration": {
                        "required_qualification_ids": sorted(
                            shift.qualifications.values_list("pk", flat=True)
                        ),
                        "minimum_number_of_participants": shift.minimum,
                        "maximum_number_of_participants": shift.maximum,
                    },
                }
            )
        result.append({"date": day, "shifts": entries})
    return result


def event_form(user, template):
    visible = list(template.visible_for.values_list("pk", flat=True))
    allowed = get_objects_for_user(
        user, "publish_event_for_group", klass=template.visible_for.model
    )
    if not visible or set(visible) - set(allowed.values_list("pk", flat=True)):
        raise PermissionDenied(_("You cannot publish events for the template's groups."))
    data = {
        "title": template.title,
        "description": template.description,
        "location": template.location,
        "visible_for": visible,
        "responsible_groups": list(template.responsible_groups.values_list("pk", flat=True)),
        "responsible_users": sorted(
            set([user.pk, *template.responsible_users.values_list("pk", flat=True)])
        ),
    }
    form = EventForm(user=user, eventtype=template.event_type, data=data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    return form, data


def create_period(
    user, *, template_id, start_date, end_date, dates, rules, creation_key, next_reminder_on=None
):
    with transaction.atomic():
        user = UserProfile.objects.select_for_update().get(pk=user.pk)
        if not enabled() or not can_plan(user) or not user.has_perm("core.add_event"):
            raise PermissionDenied
        command = dict(
            template_id=template_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            dates=sorted(day.isoformat() for day in dates),
            rules=rules,
        )
        digest = hashlib.sha256(json.dumps(command, sort_keys=True).encode()).hexdigest()
        existing = PlanningPeriod.objects.filter(creation_key=creation_key).first()
        if existing:
            if existing.created_by_id != user.pk:
                raise PermissionDenied
            if existing.request_digest != digest:
                raise Conflict(_("This creation key was already used for a different selection."))
            return existing
        if (
            not dates
            or len(set(dates)) != len(dates)
            or any(not start_date <= day <= end_date for day in dates)
        ):
            raise ValidationError(_("Select distinct dates within the period."))
        configuration = PlanningSettings(**rules)
        configuration.full_clean(validate_unique=False, validate_constraints=False)
        template = ServiceTemplate.objects.select_for_update().get(pk=template_id)
        if "manual" not in {flow.slug for flow in enabled_signup_flows()} or "uniform" not in {
            structure.slug for structure in enabled_shift_structures()
        }:
            raise ValidationError(
                _("Enable the native manual signup flow and uniform shift structure.")
            )
        preview = preview_template(template, sorted(dates), settings.TIME_ZONE)
        if any(shift["start_time"] <= timezone.now() for day in preview for shift in day["shifts"]):
            raise ValidationError(_("All shifts in a new period must start in the future."))
        _validated_form, event_data = event_form(user, template)
        period = PlanningPeriod.objects.create(
            template=template,
            start_date=start_date,
            end_date=end_date,
            timezone=settings.TIME_ZONE,
            rules=rules,
            template_snapshot={**event_data, "event_type": template.event_type_id},
            creation_key=creation_key,
            next_reminder_on=next_reminder_on,
            request_digest=digest,
            created_by=user,
        )
        for day in preview:
            form, _data = event_form(user, template)
            event = form.save()
            link = PlannedEvent.objects.create(
                period=period, date=day["date"], event=event, original_event_id=event.pk
            )
            for entry in day["shifts"]:
                shift = Shift(
                    event=event,
                    **entry,
                    signup_flow_slug="manual",
                    signup_flow_configuration={"no_selfservice_explanation": ""},
                    structure_slug="uniform",
                )
                shift.full_clean()
                shift.save()
                PlannedShift.objects.create(
                    event=link,
                    shift=shift,
                    original_shift_id=shift.pk,
                    snapshot=json.loads(json.dumps(entry, default=str)),
                )
            event.activate()
        return period
