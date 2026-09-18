"""Business messages: survey, published plan, staffing, assemblies and reminders."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from ephios.core.models import AbstractParticipation, Shift
from ephios.core.models.users import Notification
from ephios.core.services.notifications.types import AbstractNotificationHandler
from ephios.core.templatetags.settings_extras import make_absolute

from .models import (
    Assembly,
    ObserverParticipation,
    PlannedShift,
    PlanningPeriod,
    SurveyResponse,
)
from .surveys import recommendation, response_is_actionable

CONFIRMED = AbstractParticipation.States.CONFIRMED


def period_label(period):
    """The period is its date range: the service name is already in every subject line."""
    return "{start} – {end}".format(
        start=date_format(period.start_date, "SHORT_DATE_FORMAT"),
        end=date_format(period.end_date, "SHORT_DATE_FORMAT"),
    )


def local_time(value, period, fmt="DATETIME_FORMAT"):
    return date_format(timezone.localtime(value, ZoneInfo(period.timezone)), fmt)


def shift_label(period, snapshot):
    return "{start} – {end} · {label}".format(
        start=local_time(datetime.fromisoformat(snapshot["start_time"]), period),
        end=local_time(datetime.fromisoformat(snapshot["end_time"]), period, "TIME_FORMAT"),
        label=snapshot["label"],
    )


def text(lines):
    """Join message lines; blank lines separate paragraphs and lists in mail and web."""
    return "\n".join(str(line) for line in lines)


class SurveyInvitation(AbstractNotificationHandler):
    slug = "shift_coordination_invitation"
    title = _("Invitation to an availability survey for a service plan")

    @classmethod
    def response(cls, notification):
        return (
            SurveyResponse.objects.select_related("period")
            .filter(period_id=notification.data["period_id"], user_id=notification.user_id)
            .first()
        )

    @classmethod
    def get_subject(cls, notification):
        response = cls.response(notification)
        if response is None:
            return str(cls.title)
        return _("Service planning {period}: when can you help?").format(
            period=period_label(response.period)
        )

    @classmethod
    def opening(cls, response):
        return _(
            "we are planning the services from {period}. Please tell us by {deadline} when "
            "you could take a shift."
        ).format(
            period=period_label(response.period),
            deadline=local_time(response.period.deadline, response.period),
        )

    @classmethod
    def get_body(cls, notification):
        response = cls.response(notification)
        if response is None:
            return ""
        period = response.period
        lines = [
            _("Hello {name},").format(name=notification.user.get_full_name()),
            "",
            cls.opening(response),
            "",
            _("- Rate every shift: unavailable, if needed, available or preferred."),
            _("- Your personal maximum is the number of shifts we may assign to you at most."),
        ]
        if recommendation(period.suggestion):
            lines.append(f"- {recommendation(period.suggestion)}")
        if period.rules.get("allow_observers"):
            lines.append(
                _("- You can also offer to sit in on a service without taking a shift yourself.")
            )
        lines += [
            "",
            _(
                "Your answer is not an assignment yet. Once the plan is published you will get "
                "a message with the shifts you are staffed for."
            ),
            _("You can change your answer until the deadline."),
        ]
        return text(lines)

    @classmethod
    def get_actions(cls, notification):
        return [
            (
                str(_("Enter your availability")),
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
        response = cls.response(notification)
        return response is None or not response_is_actionable(response)


class SurveyReminder(SurveyInvitation):
    slug = "shift_coordination_reminder"
    title = _("Reminder about an unanswered availability survey")

    @classmethod
    def get_subject(cls, notification):
        response = cls.response(notification)
        if response is None:
            return str(cls.title)
        return _("Reminder: your availability for {period} is still missing").format(
            period=period_label(response.period)
        )

    @classmethod
    def opening(cls, response):
        return _(
            "we are still missing your availability for the services from {period}. "
            "The survey is open until {deadline}."
        ).format(
            period=period_label(response.period),
            deadline=local_time(response.period.deadline, response.period),
        )

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
    title = _("Your shifts in a published service plan")

    @classmethod
    def entries(cls, notification):
        from ephios.core.models import AbstractParticipation, Shift, UserProfile

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
        people = {m["user_id"] for record in records for m in record["members"]}
        names = {user.pk: str(user) for user in UserProfile.objects.filter(pk__in=people)}
        viewable = dict(
            AbstractParticipation.objects.filter(
                pk__in=[m["participation_id"] for record in records for m in record["members"]]
            )
            .viewable_by(notification.user.as_participant())
            .values_list("pk", "localparticipation__user_id")
        )
        entries = []
        for record in records:
            shift = shifts.get(record["native_id"])
            if not shift or not notification.user.has_perm("core.view_event", shift.event):
                continue
            partners = []
            for member in record["members"]:
                expected = None if member.get("observer") else member["user_id"]
                if (
                    member["user_id"] == notification.user_id
                    or viewable.get(member["participation_id"], False) != expected
                    or member["user_id"] not in names
                ):
                    continue
                partners.append(names[member["user_id"]])
            entries.append(
                {
                    "record": record,
                    "url": shift.get_absolute_url(),
                    "partners": partners,
                    "observer": any(
                        m["user_id"] == notification.user_id and m.get("observer")
                        for m in record["members"]
                    ),
                }
            )
        return period, entries

    @classmethod
    def get_subject(cls, notification):
        period = PlanningPeriod.objects.filter(pk=notification.data["period_id"]).first()
        if period is None:
            return str(cls.title)
        return _("Service plan for {period} published").format(period=period_label(period))

    @classmethod
    def get_body(cls, notification):
        period, entries = cls.entries(notification)
        if not period:
            return ""
        lines = [
            _("Hello {name},").format(name=notification.user.get_full_name()),
            "",
            _("the service plan for {period} has just been published. Your shifts:").format(
                period=period_label(period),
            ),
            "",
        ]
        for entry in entries:
            details = [f"[{shift_label(period, entry['record'])}]({make_absolute(entry['url'])})"]
            if entry["observer"]:
                details.append(str(_("sitting in, no working hours")))
            if entry["partners"]:
                details.append(str(_("with {names}").format(names=", ".join(entry["partners"]))))
            lines.append("- " + " · ".join(details))
        calendar = make_absolute(reverse("core:settings_calendar"))
        lines += [
            "",
            _(
                "Every shift links to its service. They are also in your personal "
                "[ephios calendar]({calendar})."
            ).format(calendar=calendar),
            _(
                "If you cannot make it, please sign off in ephios as early as you can so that "
                "we can find a replacement."
            ),
            _("This message records the published plan; ephios always shows the current one."),
        ]
        return text(lines)

    @classmethod
    def get_actions(cls, notification):
        return []

    @classmethod
    def is_obsolete(cls, notification):
        from .access import enabled

        return not enabled() or not cls.entries(notification)[1]


class ShiftStaffingHandler(AbstractNotificationHandler):
    @classmethod
    def planned_shift(cls, notification):
        return (
            PlannedShift.objects.filter(pk=notification.data["planned_shift_id"])
            .select_related("shift__event", "event__period")
            .first()
        )

    @classmethod
    def describe(cls, planned):
        period = planned.event.period
        return "{title}: {shift}".format(
            title=period.template_snapshot["title"],
            shift=shift_label(period, planned.snapshot),
        )

    @classmethod
    def get_actions(cls, notification):
        planned = cls.planned_shift(notification)
        if planned is None or planned.shift is None:
            return []
        return [
            (
                str(_("Open the service")),
                make_absolute(planned.shift.event.get_absolute_url()),
            ),
            (
                str(_("Open the replacement overview")),
                make_absolute(
                    reverse("ephios_shift_coordination:replacement", args=[planned.event_id])
                ),
            ),
        ]


class ShiftUnderstaffed(ShiftStaffingHandler):
    slug = "shift_coordination_understaffed"
    title = _("A staffed shift of your service plan lost a person")

    @classmethod
    def get_subject(cls, notification):
        planned = cls.planned_shift(notification)
        if planned is None:
            return str(cls.title)
        return _("Shift no longer staffed: {shift}").format(shift=cls.describe(planned))

    @classmethod
    def get_body(cls, notification):
        from .staffing import staffing

        planned = cls.planned_shift(notification)
        if planned is None or planned.shift is None:
            return ""
        state = staffing(planned)
        return text(
            [
                _("{person} signed off from {shift}.").format(
                    person=notification.data.get("person", ""), shift=cls.describe(planned)
                ),
                "",
                _("The shift now has {count} of at least {minimum} people.").format(
                    count=state["count"], minimum=state["minimum"]
                ),
                "",
                _(
                    "The replacement overview shows who answered that they are available that day "
                    "and who could take the shift."
                ),
            ]
        )

    @classmethod
    def is_obsolete(cls, notification):
        from .staffing import staffing

        planned = cls.planned_shift(notification)
        return planned is None or planned.shift is None or staffing(planned)["sufficient"]


class StaffingChanged(ShiftStaffingHandler):
    slug = "shift_coordination_staffing"
    title = _("A coordinator changed your staffing")

    @classmethod
    def get_subject(cls, notification):
        planned = cls.planned_shift(notification)
        if planned is None:
            return str(cls.title)
        if notification.data["change"] == "assigned":
            return _("You are staffed for {shift}").format(shift=cls.describe(planned))
        return _("You are no longer staffed for {shift}").format(shift=cls.describe(planned))

    @classmethod
    def get_body(cls, notification):
        planned = cls.planned_shift(notification)
        if planned is None or planned.shift is None:
            return ""
        actor = notification.data.get("actor", "")
        if notification.data["change"] == "assigned":
            lines = [
                _("{actor} staffed you for {shift}.").format(
                    actor=actor, shift=cls.describe(planned)
                )
            ]
            if notification.data.get("observer"):
                lines.append(
                    _(
                        "You are sitting in: no qualification is required and the time is not "
                        "counted as working hours."
                    )
                )
            lines += [
                "",
                _("If that does not work for you, please sign off in ephios as early as you can."),
            ]
            return text(lines)
        return text(
            [
                _("{actor} removed you from {shift}.").format(
                    actor=actor, shift=cls.describe(planned)
                ),
                "",
                _("You do not have to do anything. Your other shifts are unchanged."),
            ]
        )

    @classmethod
    def is_obsolete(cls, notification):
        from .staffing import staffing

        planned = cls.planned_shift(notification)
        if planned is None or planned.shift is None:
            return True
        staffed = any(
            row["user"] and row["user"].pk == notification.user_id
            for row in staffing(planned)["rows"]
        )
        return staffed != (notification.data["change"] == "assigned")


class AssemblyInvitation(AbstractNotificationHandler):
    slug = "shift_coordination_assembly_invitation"
    title = _("Invitation to an assembly")

    @classmethod
    def send(cls, assembly, recipients):
        for user in recipients:
            Notification.objects.create(user=user, slug=cls.slug, data={"assembly_id": assembly.pk})

    @classmethod
    def assembly(cls, notification):
        return (
            Assembly.objects.filter(pk=notification.data["assembly_id"])
            .select_related("event")
            .first()
        )

    @classmethod
    def when(cls, assembly):
        shift = assembly.event.shifts.first()
        if shift is None:
            return ""
        return "{start} – {end}".format(
            start=date_format(timezone.localtime(shift.start_time), "DATETIME_FORMAT"),
            end=date_format(timezone.localtime(shift.end_time), "TIME_FORMAT"),
        )

    @classmethod
    def link(cls, assembly, user):
        from .assemblies import answer_link

        return make_absolute(
            reverse(
                "ephios_shift_coordination:assembly_respond", args=[answer_link(assembly, user)]
            )
        )

    @classmethod
    def status_line(cls, assembly, user):
        from .assemblies import own_state

        state = own_state(assembly, user)
        if state == AbstractParticipation.States.CONFIRMED:
            return _("You have said yes so far.")
        if state == AbstractParticipation.States.USER_DECLINED:
            return _("You have said no so far.")
        return _("You have not answered yet.")

    @classmethod
    def get_subject(cls, notification):
        assembly = cls.assembly(notification)
        if assembly is None:
            return str(cls.title)
        return _("Invitation: {title}, {when}").format(
            title=assembly.event.title, when=cls.when(assembly)
        )

    @classmethod
    def get_body(cls, notification):
        assembly = cls.assembly(notification)
        if assembly is None:
            return ""
        event = assembly.event
        lines = [
            _("Hello {name},").format(name=notification.user.get_full_name()),
            "",
            _("you are invited to {title} on {when}.").format(
                title=event.title, when=cls.when(assembly)
            ),
        ]
        if event.location:
            lines.append(_("Where: {location}").format(location=event.location))
        if event.description:
            lines += ["", event.description]
        if assembly.agenda:
            lines += ["", _("Agenda:"), ""]
            lines += [f"- {item.strip()}" for item in assembly.agenda.splitlines() if item.strip()]
        lines += [
            "",
            cls.status_line(assembly, notification.user),
            _("Please tell us whether you can come: [answer here]({link}).").format(
                link=cls.link(assembly, notification.user)
            ),
        ]
        return text(lines)

    @classmethod
    def get_actions(cls, notification):
        assembly = cls.assembly(notification)
        if assembly is None:
            return []
        return [(str(_("Answer the invitation")), cls.link(assembly, notification.user))]

    @classmethod
    def is_obsolete(cls, notification):
        from .access import enabled

        assembly = cls.assembly(notification)
        shift = assembly.event.shifts.first() if assembly else None
        return not enabled() or shift is None or shift.end_time <= timezone.now()


class ServiceReminder(AbstractNotificationHandler):
    """Shortly before a service: who is on duty, when, where and with whom."""

    slug = "shift_coordination_service_reminder"
    title = _("Reminder about a service you are staffed for")

    @classmethod
    def shift(cls, notification):
        return (
            Shift.objects.filter(pk=notification.data["shift_id"]).select_related("event").first()
        )

    @classmethod
    def when(cls, shift):
        return "{start} – {end}".format(
            start=date_format(timezone.localtime(shift.start_time), "DATETIME_FORMAT"),
            end=date_format(timezone.localtime(shift.end_time), "TIME_FORMAT"),
        )

    @classmethod
    def get_subject(cls, notification):
        shift = cls.shift(notification)
        if shift is None:
            return str(cls.title)
        return _("Reminder: {title}, {when}").format(title=shift.event.title, when=cls.when(shift))

    @classmethod
    def partners(cls, shift, notification):
        """Only the people this person may see, named the way the plugin names them."""
        observers = {
            record.participation_id: record.user
            for record in ObserverParticipation.objects.filter(
                planned_shift__shift=shift
            ).select_related("user")
        }
        names = []
        for participation in AbstractParticipation.objects.filter(
            shift=shift, state=CONFIRMED
        ).viewable_by(notification.user.as_participant()):
            person = observers.get(participation.pk)
            identifier = person.pk if person else getattr(participation, "user_id", None)
            if identifier == notification.user_id:
                continue
            names.append(str(person) if person else str(participation.participant))
        return sorted(names)

    @classmethod
    def sits_in(cls, shift, notification):
        return ObserverParticipation.objects.filter(
            planned_shift__shift=shift, user_id=notification.user_id
        ).exists()

    @classmethod
    def get_body(cls, notification):
        shift = cls.shift(notification)
        if shift is None or not notification.user.has_perm("core.view_event", shift.event):
            return ""
        lines = [
            _("Hello {name},").format(name=notification.user.get_full_name()),
            "",
            _("a reminder: you are staffed for {title} on {when}.").format(
                title=shift.event.title, when=cls.when(shift)
            ),
        ]
        if shift.event.location:
            lines.append(_("Where: {location}").format(location=shift.event.location))
        if cls.sits_in(shift, notification):
            lines.append(
                _(
                    "You are sitting in: no qualification is required and the time is not "
                    "counted as working hours."
                )
            )
        if partners := cls.partners(shift, notification):
            lines += ["", _("With you: {people}.").format(people=", ".join(partners))]
        lines += [
            "",
            _("If you cannot make it, please sign off in ephios as early as you can."),
            f"[{shift.event.title}]({make_absolute(shift.event.get_absolute_url())})",
        ]
        return text(lines)

    @classmethod
    def get_actions(cls, notification):
        return []

    @classmethod
    def is_obsolete(cls, notification):
        from .access import enabled

        shift = cls.shift(notification)
        return not enabled() or shift is None or shift.start_time <= timezone.now()


class AssemblyReminder(AssemblyInvitation):
    """The same message as the invitation, sent again shortly before the assembly."""

    slug = "shift_coordination_assembly_reminder"
    title = _("Reminder about an assembly you are invited to")

    @classmethod
    def get_subject(cls, notification):
        assembly = cls.assembly(notification)
        if assembly is None:
            return str(cls.title)
        return _("Reminder: {title}, {when}").format(
            title=assembly.event.title, when=cls.when(assembly)
        )
