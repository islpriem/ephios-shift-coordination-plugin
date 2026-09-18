"""Optionally keep the working hours out of sight for everybody but the administration.

ephios offers no hook to remove a navigation entry or to guard a core view, so this does two
things that complement each other: it hides the entries with a small style rule injected into
every page, and it turns the working hour views themselves into 403 for everybody who is not
staff. The second part wraps the core views once at startup. That is a deliberate reach into
ephios: without it the pages stay reachable by typing their address, which is exactly what the
setting is supposed to prevent. It is written so that an ephios that no longer has these views
simply keeps working.
"""

from django.core.exceptions import PermissionDenied
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .access import enabled
from .models import PlanningSettings

VIEWS = (
    "OwnWorkingHourView",
    "UserProfileWorkingHourView",
    "UserProfileWorkingHourExportView",
    "WorkingHourOverview",
    "WorkingHourExportView",
    "WorkingHourRequestView",
    "WorkingHourCreateView",
    "WorkingHourUpdateView",
    "WorkingHourDeleteView",
)
# Every link ephios writes to the working hours points below this address.
RULE = mark_safe(  # noqa: S308 - a constant rule, nothing in it comes from a request
    'a[href^="/workinghours/"] { display: none !important; }'
)


def hidden():
    """True when the working hours are configured to be out of sight."""
    settings = PlanningSettings.objects.first()
    return bool(enabled() and settings and settings.hide_working_hours)


def hidden_for(user):
    return hidden() and not (user.is_authenticated and user.is_staff)


def style(request):
    """A rule that removes the working hour links ephios writes into every page itself.

    ephios allows inline styles only with the request's nonce, so the tag carries it.
    """
    if not hidden_for(request.user):
        return ""
    return format_html('<style nonce="{}">{}</style>', getattr(request, "csp_nonce", ""), RULE)


def guard(view_class):
    """Refuse the view while the setting is on; administrators keep their access."""
    original = view_class.dispatch

    def dispatch(self, request, *args, **kwargs):
        if hidden_for(request.user):
            raise PermissionDenied
        return original(self, request, *args, **kwargs)

    dispatch.shift_coordination_guarded = True
    view_class.dispatch = dispatch
    return view_class


def guard_core_views():
    """Called once from the app's ready(); doing nothing is the safe outcome."""
    try:
        from ephios.core.views import workinghours
    except ImportError:  # pragma: no cover - only if ephios drops the views entirely
        return
    for name in VIEWS:
        view_class = getattr(workinghours, name, None)
        if view_class is not None and not getattr(
            view_class.dispatch, "shift_coordination_guarded", False
        ):
            guard(view_class)
