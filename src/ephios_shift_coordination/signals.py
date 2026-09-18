from django.dispatch import receiver
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from ephios.core.signals import (
    HTML_EVENT_INFO,
    HTML_HEAD,
    insert_html,
    nav_link,
    periodic_signal,
    register_group_permission_fields,
    register_notification_types,
    settings_sections,
)
from ephios.core.views.settings import SETTINGS_MANAGEMENT_SECTION_KEY
from ephios.extra.permissions import PermissionField

from .access import PERMISSION, can_plan
from .models import PlannedEvent, SurveyResponse


@receiver(nav_link, dispatch_uid="shift_coordination.navigation")
def navigation(sender, request, **kwargs):
    if not request.user.is_authenticated or not request.user.is_active:
        return []
    surveys = reverse("ephios_shift_coordination:survey_list")
    periods = reverse("ephios_shift_coordination:period_list")
    assemblies = reverse("ephios_shift_coordination:assembly_list")
    links = []
    if can_plan(request.user):
        # Coordinators have two places to go, so they get a menu; everybody else only ever
        # needs the surveys and is better served by one click than by a menu with one entry.
        links += [
            {
                "label": _("Availability surveys"),
                "url": surveys,
                "active": request.path == surveys,
                "group": _("Shift coordination"),
            },
            {
                "label": _("Planning periods"),
                "url": periods,
                "active": request.path.startswith(periods),
                "group": _("Shift coordination"),
            },
        ]
    else:
        links.append(
            {"label": _("Availability surveys"), "url": surveys, "active": request.path == surveys}
        )
    links.append(
        {"label": _("Assemblies"), "url": assemblies, "active": request.path.startswith(assemblies)}
    )
    return links


@receiver(settings_sections, dispatch_uid="shift_coordination.settings")
def settings_links(sender, request, **kwargs):
    if not request.user.is_active or not request.user.is_staff:
        return []
    return [
        {
            "label": label,
            "url": reverse(f"ephios_shift_coordination:{name}"),
            "active": request.path == reverse(f"ephios_shift_coordination:{name}"),
            "group": SETTINGS_MANAGEMENT_SECTION_KEY,
        }
        for name, label in [
            ("settings", _("Planning settings")),
            ("template_list", _("Service templates")),
        ]
    ]


@receiver(register_group_permission_fields, dispatch_uid="shift_coordination.group_permissions")
def permission_fields(sender, **kwargs):
    return [
        (
            "manage_planning",
            PermissionField(
                label=_("Manage shift planning"),
                disabled=True,
                help_text=_(
                    "Administrators assign this permission in Planning settings. "
                    "Native event creation and group publishing rights are also required."
                ),
                permissions=[PERMISSION],
            ),
        )
    ]


@receiver(insert_html, sender=HTML_HEAD, dispatch_uid="shift_coordination.head")
def head(sender, request, **kwargs):
    from .working_hours import style

    return style(request)


@receiver(insert_html, sender=HTML_EVENT_INFO, dispatch_uid="shift_coordination.event_info")
def event_info(sender, request, event, **kwargs):
    from .models import PlanningPeriod
    from .staffing import options, service_shifts

    if not request.user.has_perm("core.view_event", event):
        return ""
    link = PlannedEvent.objects.select_related("period").filter(event=event).first()
    if not link:
        return assembly_info(request, event)
    shifts = []
    if link.period.state == PlanningPeriod.State.PUBLISHED:
        shifts = [options(request.user, planned) for planned in service_shifts(link)]
    coordinator = can_plan(request.user)
    return render_to_string(
        "ephios_shift_coordination/event_info.html",
        {
            "period": link.period,
            "service": link,
            "can_plan": coordinator,
            "response": SurveyResponse.objects.filter(
                period=link.period, user=request.user
            ).first(),
            "shifts": shifts,
            "replacement": coordinator or any(state["mine"] for state in shifts),
        },
        request=request,
    )


def assembly_info(request, event):
    """The agenda and the invitation state, shown on the native event page."""
    from .assemblies import answer_state, attendance, shift_of
    from .forms import MinutesForm
    from .models import Assembly
    from .reminders import overview

    assembly = Assembly.objects.filter(event=event).first()
    if not assembly:
        return ""
    responsible = request.user.has_perm("core.change_event", event)
    return render_to_string(
        "ephios_shift_coordination/assembly_info.html",
        {
            "assembly": assembly,
            "agenda": [line.strip() for line in assembly.agenda.splitlines() if line.strip()],
            "answer": answer_state(assembly, request.user),
            "responsible": responsible,
            "attendance": attendance(assembly) if responsible else None,
            "minutes": assembly.minutes.all(),
            "minutes_form": MinutesForm(),
            "reminders": overview(shift_of(assembly))
            if responsible and shift_of(assembly)
            else None,
        },
        request=request,
    )


@receiver(register_notification_types, dispatch_uid="shift_coordination.notifications")
def notification_types(sender, **kwargs):
    from .notifications import (
        AssemblyInvitation,
        AssemblyReminder,
        NextPeriodDue,
        PlanPublished,
        ServiceReminder,
        ShiftUnderstaffed,
        StaffingChanged,
        SurveyInvitation,
        SurveyReminder,
    )

    return [
        SurveyInvitation,
        SurveyReminder,
        PlanPublished,
        ShiftUnderstaffed,
        StaffingChanged,
        AssemblyInvitation,
        AssemblyReminder,
        ServiceReminder,
        NextPeriodDue,
    ]


@receiver(periodic_signal, dispatch_uid="shift_coordination.survey_periodic")
def survey_periodic(sender, **kwargs):
    from django.db import transaction
    from ephios.core.services.notifications.backends import send_all_notifications

    from .reminders import process_period_reminders, process_reminders
    from .surveys import process_surveys

    process_surveys()
    process_reminders()
    process_period_reminders()
    transaction.on_commit(send_all_notifications)
