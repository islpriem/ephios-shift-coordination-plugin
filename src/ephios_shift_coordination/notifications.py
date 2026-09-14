from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from ephios.core.services.notifications.types import AbstractNotificationHandler
from ephios.core.templatetags.settings_extras import make_absolute

from .models import SurveyResponse
from .surveys import response_is_actionable


class SurveyInvitation(AbstractNotificationHandler):
    slug = "shift_coordination_invitation"
    title = _("Invitation to an availability survey")

    @classmethod
    def get_subject(cls, notification):
        return cls.title

    @classmethod
    def get_body(cls, notification):
        response = SurveyResponse.objects.select_related("period").get(
            period_id=notification.data["period_id"], user_id=notification.user_id
        )
        return _(
            "Please submit your availability by {deadline}. Preferences are not assignments."
        ).format(
            deadline=date_format(
                timezone.localtime(response.period.deadline, ZoneInfo(response.period.timezone)),
                "DATETIME_FORMAT",
            )
        )

    @classmethod
    def get_actions(cls, notification):
        return [
            (
                str(_("Open your survey")),
                make_absolute(
                    reverse(
                        "ephios_shift_coordination:survey_detail",
                        args=[notification.data["period_id"]],
                    )
                ),
            )
        ]

    @classmethod
    def is_obsolete(cls, notification):
        response = (
            SurveyResponse.objects.select_related("period", "user")
            .filter(period_id=notification.data["period_id"], user_id=notification.user_id)
            .first()
        )
        return response is None or not response_is_actionable(response)


class SurveyReminder(SurveyInvitation):
    slug = "shift_coordination_reminder"
    title = _("Reminder: your availability survey is still unanswered")

    @classmethod
    def is_obsolete(cls, notification):
        from .models import NotificationDispatch

        return (
            super().is_obsolete(notification)
            or NotificationDispatch.objects.filter(
                period_id=notification.data["period_id"],
                user_id=notification.user_id,
                kind="reminder",
                skipped=False,
                key__gt=notification.data["dispatch_key"],
            ).exists()
        )
