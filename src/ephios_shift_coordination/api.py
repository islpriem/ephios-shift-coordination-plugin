"""Three public questions about duty, for a display at the station.

The endpoints answer without a session and are switched off until somebody turns them on.
They never return anything about people: whether a duty runs, when the next one starts and
which weekdays carry one is all a wall display needs.
"""

from datetime import timedelta

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from ephios.core.models import Shift

from .access import enabled
from .models import PlanningSettings

# A display does not need to look further ahead than the published plans reach.
HORIZON = timedelta(days=90)


def configuration():
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    if not enabled() or not settings.api_enabled:
        raise Http404
    return settings


def staffed(shifts):
    """Only shifts that reach their minimum staffing count as duty."""
    return [shift for shift in shifts if shift.structure.get_signup_stats().missing == 0]


def selected(settings):
    return Shift.objects.filter(
        event__active=True, event__type__in=settings.api_event_types.all()
    ).prefetch_related("participations")


def week_window(now):
    """Monday to Sunday of the local week the request falls into."""
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = start - timedelta(days=start.weekday())
    return monday, monday + timedelta(days=7)


@login_not_required
@require_http_methods(["GET"])
def now(request):
    settings = configuration()
    moment = timezone.now()
    running = staffed(selected(settings).filter(start_time__lte=moment, end_time__gt=moment))
    return JsonResponse(
        {
            "duty": bool(running),
            "until": max(shift.end_time for shift in running).isoformat() if running else None,
        }
    )


@login_not_required
@require_http_methods(["GET"])
def next_duty(request):
    settings = configuration()
    moment = timezone.now()
    upcoming = staffed(
        selected(settings)
        .filter(start_time__gt=moment, start_time__lte=moment + HORIZON)
        .order_by("start_time")
    )
    return JsonResponse({"start": upcoming[0].start_time.isoformat() if upcoming else None})


@login_not_required
@require_http_methods(["GET"])
def week(request):
    settings = configuration()
    monday, sunday = week_window(timezone.now())
    days = [False] * 7
    for shift in staffed(selected(settings).filter(start_time__gte=monday, start_time__lt=sunday)):
        days[timezone.localtime(shift.start_time).weekday()] = True
    return JsonResponse({"from": monday.date().isoformat(), "days": days})
