from django.dispatch import receiver
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from ephios.core.signals import (
    HTML_EVENT_INFO,
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
    links = []
    if can_plan(request.user):
        links.append(
            {
                "label": _("Shift coordination"),
                "url": reverse("ephios_shift_coordination:period_list"),
                "active": request.path.startswith(reverse("ephios_shift_coordination:period_list")),
            }
        )
    links.append(
        {
            "label": _("Surveys"),
            "url": reverse("ephios_shift_coordination:survey_list"),
            "active": request.path == reverse("ephios_shift_coordination:survey_list"),
        }
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


@receiver(insert_html, sender=HTML_EVENT_INFO, dispatch_uid="shift_coordination.event_info")
def event_info(sender, request, event, **kwargs):
    link = PlannedEvent.objects.select_related("period").filter(event=event).first()
    if not link or not request.user.has_perm("core.view_event", event):
        return ""
    return render_to_string(
        "ephios_shift_coordination/event_info.html",
        {
            "period": link.period,
            "can_plan": can_plan(request.user),
            "response": SurveyResponse.objects.filter(
                period=link.period, user=request.user
            ).first(),
        },
    )


@receiver(register_notification_types, dispatch_uid="shift_coordination.notifications")
def notification_types(sender, **kwargs):
    from .notifications import PlanPublished, SurveyInvitation, SurveyReminder

    return [SurveyInvitation, SurveyReminder, PlanPublished]


@receiver(periodic_signal, dispatch_uid="shift_coordination.survey_periodic")
def survey_periodic(sender, **kwargs):
    from django.db import transaction
    from ephios.core.services.notifications.backends import send_all_notifications

    from .surveys import process_surveys

    process_surveys()
    transaction.on_commit(send_all_notifications)
