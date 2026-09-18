"""Assembly minutes: a PDF per assembly, filed by the responsibles and readable by everybody
who may see that assembly.

The uploaded file name is thrown away. It often carries the writer's folder habits, and a
generated name keeps the media directory predictable; what people see is built from the
assembly instead. Finding minutes again is finding their assembly, so the search lives with
the assemblies.
"""

from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from .access import enabled
from .models import AssemblyMinutes


def may_file(user, assembly):
    return enabled() and user.has_perm("core.change_event", assembly.event)


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
