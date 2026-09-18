"""Assembly minutes: a PDF per assembly, filed by the responsibles and readable by everybody
who may see that assembly.

The uploaded file name is thrown away. It often carries the writer's folder habits, and a
generated name keeps the media directory predictable; what people see is built from the
assembly instead.
"""

from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from ephios.core.models import Event
from guardian.shortcuts import get_objects_for_user

from .access import enabled
from .models import AssemblyMinutes


def may_file(user, assembly):
    return enabled() and user.has_perm("core.change_event", assembly.event)


def visible(user, search=""):
    """Minutes of assemblies this person may see, newest assembly first."""
    events = get_objects_for_user(user, "core.view_event", klass=Event)
    found = AssemblyMinutes.objects.filter(assembly__event__in=events).select_related(
        "assembly__event", "assembly__event__type", "uploaded_by"
    )
    for word in search.split():
        found = found.filter(matching(word))
    return found.order_by("-assembly__event__shifts__start_time", "-pk").distinct()


def as_day(word):
    """A written date, in the ISO or the German notation, or nothing."""
    if day := parse_date(word):
        return day
    try:
        return datetime.strptime(word, "%d.%m.%Y").date()
    except ValueError:
        return None


def matching(word):
    """Text matches title, kind and agenda; a year or a date matches the appointment."""
    found = (
        Q(assembly__event__title__icontains=word)
        | Q(assembly__event__type__title__icontains=word)
        | Q(assembly__agenda__icontains=word)
    )
    if word.isdigit() and len(word) == 4:
        found |= Q(assembly__event__shifts__start_time__year=int(word))
    if day := as_day(word):
        found |= Q(assembly__event__shifts__start_time__date=day)
    return found


def display_name(minutes):
    """What a browser shows as the file name: the assembly, not the upload."""
    shift = minutes.assembly.event.shifts.first()
    day = date_format(timezone.localtime(shift.start_time), "Y-m-d") if shift else "ohne-datum"
    return f"{_('assembly-minutes')}-{day}.pdf"


def file_minutes(user, assembly, upload):
    if not may_file(user, assembly):
        raise PermissionDenied
    return AssemblyMinutes.objects.create(assembly=assembly, file=upload, uploaded_by=user)


def remove(user, minutes):
    if not may_file(user, minutes.assembly):
        raise PermissionDenied
    minutes.delete()
