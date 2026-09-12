from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404
from ephios.core.plugins import get_enabled_plugins

PERMISSION = "ephios_shift_coordination.manage_planning"


def enabled():
    return any(plugin.module == "ephios_shift_coordination" for plugin in get_enabled_plugins())


def can_plan(user):
    return user.is_authenticated and user.is_active and user.has_perm(PERMISSION)


def require_access(role="coordinator"):
    def decorate(view):
        @wraps(view)
        def protected(request, *args, **kwargs):
            if not enabled():
                raise Http404
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if (
                not request.user.is_active
                or (role == "admin" and not request.user.is_staff)
                or (role == "coordinator" and not can_plan(request.user))
            ):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return protected

    return decorate
