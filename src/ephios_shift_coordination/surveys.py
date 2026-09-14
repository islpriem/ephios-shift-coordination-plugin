from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext as _
from ephios.core.models import UserProfile
from ephios.core.models.users import Notification

from .access import can_plan, enabled
from .ephios_integration import check_structure, eligible
from .models import (
    Availability,
    NotificationDispatch,
    PlanningPeriod,
    PlanningSettings,
    SurveyResponse,
)
from .services import Conflict


def dispatch(response, kind, key, *, skipped=False):
    record, created = NotificationDispatch.objects.get_or_create(
        period=response.period,
        user=response.user,
        kind=kind,
        key=key,
        defaults={"skipped": skipped},
    )
    if created and not skipped:
        record.notification = Notification.objects.create(
            user=response.user,
            slug=f"shift_coordination_{kind}",
            data={"period_id": response.period_id, "dispatch_key": key},
        )
        record.save(update_fields=["notification"])


def open_survey(
    user, period_id, *, expected_version, deadline=None, reminder_days=None, open_now=True
):
    with transaction.atomic():
        period = PlanningPeriod.objects.select_for_update().get(pk=period_id)
        user = UserProfile.objects.get(pk=user.pk)
        if not enabled() or not can_plan(user):
            raise PermissionDenied
        shifts, requirements = check_structure(period, user)
        if period.opened_at and open_now:
            return period
        if period.state != PlanningPeriod.State.PREPARATION or period.version != expected_version:
            raise Conflict(_("This period has changed. Reload before continuing."))
        now = timezone.now()
        deadline = (
            deadline or period.deadline or now + timedelta(days=period.rules["response_days"])
        )
        if not now < deadline < min(shift.shift.start_time for shift in shifts):
            raise ValidationError(
                _("The deadline must be in the future and before the first shift.")
            )
        offsets = period.rules["reminder_days"] if reminder_days is None else reminder_days
        configuration = PlanningSettings(**{**period.rules, "reminder_days": offsets})
        configuration.full_clean(validate_unique=False, validate_constraints=False)
        period.deadline = deadline
        period.rules = configuration.snapshot()
        period.version += 1
        if open_now:
            period.opened_at = now
            period.opened_structure = requirements
            period.state = PlanningPeriod.State.SURVEY_OPEN
            for member in UserProfile.objects.filter(is_active=True).prefetch_related(
                "qualification_grants"
            ):
                offered = [shift for shift in shifts if eligible(member, shift)]
                if offered:
                    response = SurveyResponse.objects.create(period=period, user=member)
                    response.offered_shifts.set(offered)
                    dispatch(response, "invitation", "opening")
        period.save()
        return period


def save_response(user, period_id, *, expected_version, maximum, notes, ratings):
    with transaction.atomic():
        period = PlanningPeriod.objects.select_for_update().get(pk=period_id)
        user = UserProfile.objects.get(pk=user.pk)
        if not enabled() or not user.is_active:
            raise PermissionDenied
        response = (
            SurveyResponse.objects.select_for_update().filter(period=period, user=user).first()
        )
        if response is None:
            raise Http404
        now = timezone.now()
        if period.state != PlanningPeriod.State.SURVEY_OPEN or now >= period.deadline:
            raise Conflict(_("The response deadline has passed. Your response is read-only."))
        if response.version != expected_version:
            raise Conflict(_("Your response has changed. Reload before saving again."))
        check_structure(period)
        offered = list(response.offered_shifts.select_related("shift__event"))
        if any(not user.has_perm("core.view_event", shift.shift.event) for shift in offered):
            raise PermissionDenied
        if (
            type(maximum) is not int
            or not 0 <= maximum <= 2147483647
            or not isinstance(notes, str)
            or len(notes) > 4000
            or not isinstance(ratings, dict)
            or any(type(pk) is not int for pk in ratings)
            or set(ratings) != {shift.pk for shift in offered}
            or any(rating not in Availability.Rating.values for rating in ratings.values())
        ):
            raise ValidationError(
                _("Provide a non-negative maximum and a rating for every offered shift.")
            )
        response.maximum, response.notes = maximum, notes
        response.submitted_at = response.submitted_at or now
        response.updated_at = now
        response.version += 1
        response.save()
        response.availabilities.all().delete()
        Availability.objects.bulk_create(
            [
                Availability(response=response, planned_shift_id=pk, rating=rating)
                for pk, rating in ratings.items()
            ]
        )
        return response


def response_is_actionable(response):
    period = response.period
    return (
        enabled()
        and response.user.is_active
        and response.submitted_at is None
        and period.state == PlanningPeriod.State.SURVEY_OPEN
        and timezone.now() < period.deadline
        and any(
            eligible(response.user, shift)
            for shift in response.offered_shifts.select_related("shift__event__type")
        )
    )


def process_surveys():
    if not enabled():
        return
    for pk in PlanningPeriod.objects.filter(state=PlanningPeriod.State.SURVEY_OPEN).values_list(
        "pk", flat=True
    ):
        with transaction.atomic():
            period = PlanningPeriod.objects.select_for_update().get(pk=pk)
            if period.state != PlanningPeriod.State.SURVEY_OPEN:
                continue
            now = timezone.now()
            if now >= period.deadline:
                period.state = PlanningPeriod.State.PLANNING
                period.version += 1
                period.save(update_fields=["state", "version"])
                continue
            due = sorted(
                period.deadline - timedelta(days=day)
                for day in period.rules["reminder_days"]
                if period.opened_at <= period.deadline - timedelta(days=day) <= now
            )
            for response in period.responses.select_related("user"):
                response.period = period
                for instant in due:
                    dispatch(
                        response,
                        "reminder",
                        f"due:{instant.isoformat()}",
                        skipped=instant != due[-1] or not response_is_actionable(response),
                    )
