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


class PlanPublished(AbstractNotificationHandler):
    slug = "shift_coordination_published"
    title = _("Your published service plan")

    @classmethod
    def get_subject(cls, notification):
        return cls.title

    @classmethod
    def entries(cls, notification):
        from ephios.core.models import AbstractParticipation, LocalParticipation, Shift

        from .models import PlanningPeriod

        period = PlanningPeriod.objects.filter(
            pk=notification.data["period_id"], state=PlanningPeriod.State.PUBLISHED
        ).first()
        if not period or not notification.user.is_active:
            return None, []
        records = [
            shift
            for shift in period.publication_snapshot["shifts"]
            if any(m["user_id"] == notification.user_id for m in shift["members"])
        ]
        shifts = (
            Shift.objects.filter(pk__in=[s["native_id"] for s in records])
            .select_related("event")
            .in_bulk()
        )
        entries = []
        for record in records:
            shift = shifts.get(record["native_id"])
            if not shift or not notification.user.has_perm("core.view_event", shift.event):
                continue
            partners = LocalParticipation.objects.filter(
                pk__in=AbstractParticipation.objects.filter(
                    pk__in=[m["participation_id"] for m in record["members"]]
                )
                .viewable_by(notification.user.as_participant())
                .values_list("pk", flat=True)
            ).select_related("user")
            expected = {(m["participation_id"], m["user_id"]) for m in record["members"]}
            entries.append(
                {
                    "record": record,
                    "url": shift.get_absolute_url(),
                    "partners": [
                        str(p.user)
                        for p in partners
                        if (p.pk, p.user_id) in expected and p.user_id != notification.user_id
                    ],
                }
            )
        return period, entries

    @classmethod
    def get_body(cls, notification):
        from datetime import datetime

        period, entries = cls.entries(notification)
        if not period:
            return ""
        zone = ZoneInfo(period.timezone)
        lines = [
            _(
                "Your service plan was published on {published}. This message records that "
                "published plan. Check ephios for current assignments."
            ).format(
                published=date_format(
                    timezone.localtime(period.published_at, zone), "DATETIME_FORMAT"
                )
            )
        ]
        for entry in entries:
            record = entry["record"]
            lines.append(
                "{label}: {start} – {end}".format(
                    label=record["label"],
                    start=date_format(
                        timezone.localtime(datetime.fromisoformat(record["start_time"]), zone),
                        "DATETIME_FORMAT",
                    ),
                    end=date_format(
                        timezone.localtime(datetime.fromisoformat(record["end_time"]), zone),
                        "DATETIME_FORMAT",
                    ),
                )
            )
            if entry["partners"]:
                lines.append(
                    _("Partners in the published plan: {names}").format(
                        names=", ".join(entry["partners"])
                    )
                )
        return "\n".join(lines)

    @classmethod
    def get_actions(cls, notification):
        _period, entries = cls.entries(notification)
        if not entries:
            return []
        return [(entry["record"]["label"], make_absolute(entry["url"])) for entry in entries] + [
            (
                str(_("Open your calendar settings")),
                make_absolute(reverse("core:settings_calendar")),
            )
        ]

    @classmethod
    def is_obsolete(cls, notification):
        from .access import enabled

        return not enabled() or not cls.entries(notification)[1]
