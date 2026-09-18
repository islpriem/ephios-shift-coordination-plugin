import uuid
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models, transaction
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .dates import holiday_calendar


def default_weekdays():
    return [1, 2, 3, 6]


def default_reminders():
    return [3]


# Settings that steer the plugin itself: they are never copied into a planning period.
INSTANCE_SETTINGS = (
    "service_reminder_days",
    "service_reminder_time",
    "assembly_reminder_days",
    "assembly_reminder_time",
    "next_period_weeks",
    "api_enabled",
)


def nine_o_clock():
    """A reminder in the morning of the chosen day, unless the instance says otherwise."""
    return time(9, 0)


class PlanningSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    weekdays = models.JSONField(_("Weekdays"), default=default_weekdays)
    country = models.CharField(_("Holiday country"), max_length=2, default="DE")
    region = models.CharField(_("Holiday region"), max_length=8, blank=True)
    exclude_holidays = models.BooleanField(_("Exclude holidays"), default=False)
    response_days = models.PositiveSmallIntegerField(
        _("Response period in days"), default=7, validators=[MinValueValidator(1)]
    )
    reminder_days = models.JSONField(
        _("Reminder days before deadline"), default=default_reminders, blank=True
    )
    weekly_limit = models.PositiveSmallIntegerField(_("Weekly limit"), default=2)
    free_next_day = models.BooleanField(_("Keep the next day free"), default=True)
    solver_seconds = models.PositiveSmallIntegerField(
        _("Calculation budget in seconds"), default=10, validators=[MinValueValidator(1)]
    )
    allow_observers = models.BooleanField(_("Allow observers"), default=False)
    minimum_regular = models.PositiveSmallIntegerField(
        _("Regular people per shift with observers"),
        default=1,
        validators=[MinValueValidator(1)],
    )
    service_reminder_days = models.JSONField(
        _("Service reminders in days before"), default=list, blank=True
    )
    service_reminder_time = models.TimeField(_("Service reminder time"), default=nine_o_clock)
    assembly_reminder_days = models.JSONField(
        _("Assembly reminders in days before"), default=list, blank=True
    )
    assembly_reminder_time = models.TimeField(_("Assembly reminder time"), default=nine_o_clock)
    next_period_weeks = models.PositiveSmallIntegerField(
        _("Remind about the next period this many weeks before the end"),
        default=2,
        validators=[MinValueValidator(1)],
    )
    api_enabled = models.BooleanField(_("Public duty information"), default=False)
    api_event_types = models.ManyToManyField(
        "core.EventType", verbose_name=_("Event types in the public information"), blank=True
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="planning_single_settings")
        ]

    def clean(self):
        holiday_calendar(self.country, self.region, self.exclude_holidays)
        if (
            not isinstance(self.weekdays, list)
            or not self.weekdays
            or any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays)
            or len(set(self.weekdays)) != len(self.weekdays)
        ):
            raise ValidationError({"weekdays": _("Choose distinct weekdays.")})
        if (
            not isinstance(self.reminder_days, list)
            or any(type(day) is not int or day <= 0 for day in self.reminder_days)
            or len(set(self.reminder_days)) != len(self.reminder_days)
        ):
            raise ValidationError({"reminder_days": _("Enter distinct positive reminder offsets.")})
        for name in ("service_reminder_days", "assembly_reminder_days"):
            offsets = getattr(self, name)
            # Zero means the day of the appointment itself, which is a sensible reminder.
            if (
                not isinstance(offsets, list)
                or any(type(day) is not int or day < 0 for day in offsets)
                or len(set(offsets)) != len(offsets)
            ):
                raise ValidationError({name: _("Enter distinct reminder offsets of zero or more.")})

    def snapshot(self):
        """The planning rules a period keeps; instance settings are not part of them."""
        return {
            field.name: getattr(self, field.name)
            for field in self._meta.fields
            if field.name != "id" and field.name not in INSTANCE_SETTINGS
        }


class ServiceTemplate(models.Model):
    title = models.CharField(_("Title"), max_length=254)
    description = models.TextField(_("Description"), blank=True)
    location = models.CharField(_("Location"), max_length=254)
    event_type = models.ForeignKey("core.EventType", models.PROTECT, verbose_name=_("Event type"))
    visible_for = models.ManyToManyField("auth.Group", verbose_name=_("Visible for"))
    responsible_groups = models.ManyToManyField(
        "auth.Group",
        blank=True,
        related_name="planning_templates",
        verbose_name=_("Responsible groups"),
    )
    responsible_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, verbose_name=_("Responsible persons")
    )

    def __str__(self):
        return self.title


class ShiftTemplate(models.Model):
    template = models.ForeignKey(ServiceTemplate, models.CASCADE, related_name="shifts")
    position = models.PositiveSmallIntegerField(default=0)
    label = models.CharField(_("Label"), max_length=255)
    meeting_time = models.TimeField(_("Meeting time"))
    start_time = models.TimeField(_("Start time"))
    end_time = models.TimeField(_("End time"))
    end_day_offset = models.PositiveSmallIntegerField(
        _("End day"), choices=[(0, _("Same day")), (1, _("Next day"))], default=0
    )
    minimum = models.PositiveSmallIntegerField(
        _("Minimum staffing"), validators=[MinValueValidator(1)]
    )
    maximum = models.PositiveSmallIntegerField(_("Maximum staffing"), null=True, blank=True)
    qualifications = models.ManyToManyField(
        "core.Qualification", verbose_name=_("Qualifications"), blank=True
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(minimum__gte=1), name="planning_shift_minimum"
            ),
            models.CheckConstraint(
                condition=models.Q(maximum__isnull=True)
                | models.Q(maximum__gte=models.F("minimum")),
                name="planning_shift_maximum",
            ),
            models.CheckConstraint(
                condition=models.Q(end_day_offset__in=[0, 1]), name="planning_shift_end_day"
            ),
        ]

    def clean(self):
        if self.minimum is not None and (
            self.minimum < 1 or (self.maximum is not None and self.maximum < self.minimum)
        ):
            raise ValidationError(_("Maximum staffing must be at least the positive minimum."))
        if self.end_day_offset not in (0, 1):
            raise ValidationError({"end_day_offset": _("Choose the same or next day.")})
        if self.meeting_time and self.start_time and self.meeting_time > self.start_time:
            raise ValidationError({"meeting_time": _("Meeting time must not be after the start.")})
        if self.start_time and self.end_time:
            start = datetime.combine(date(2000, 1, 1), self.start_time)
            end = datetime.combine(start.date(), self.end_time) + timedelta(
                days=self.end_day_offset
            )
            if end <= start:
                raise ValidationError({"end_time": _("End time must be after the start.")})


class PlanningPeriod(models.Model):
    class State(models.TextChoices):
        PREPARATION = "preparation", _("Preparation")
        SURVEY_OPEN = "survey_open", _("Survey open")
        PLANNING = "planning", _("Planning")
        PUBLISHED = "published", _("Published")

    template = models.ForeignKey(ServiceTemplate, models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    timezone = models.CharField(max_length=64)
    rules = models.JSONField()
    template_snapshot = models.JSONField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.PREPARATION)
    version = models.PositiveIntegerField(default=1)
    draft_fingerprint = models.CharField(max_length=64, blank=True)
    published_at = models.DateTimeField(null=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.SET_NULL,
        null=True,
        related_name="published_planning_periods",
    )
    publication_snapshot = models.JSONField(default=dict)
    opened_at = models.DateTimeField(null=True)
    deadline = models.DateTimeField(null=True)
    opened_structure = models.JSONField(default=dict)
    # Recommended personal maximum and the capacity estimate it is based on.
    suggestion = models.JSONField(default=dict)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
    # The day the coordinator wants to be reminded to plan the period after this one.
    next_reminder_on = models.DateField(null=True)
    request_digest = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("manage_planning", "Manage shift planning")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="planning_period_dates",
            )
        ]
        ordering = ["-start_date", "-pk"]

    @property
    def effective_state(self):
        if self.state == self.State.SURVEY_OPEN and self.deadline <= timezone.now():
            return self.State.PLANNING
        return self.state

    @property
    def effective_state_label(self):
        return self.State(self.effective_state).label

    def get_absolute_url(self):
        return reverse("ephios_shift_coordination:period_detail", args=[self.pk])


class PlannedEvent(models.Model):
    period = models.ForeignKey(PlanningPeriod, models.CASCADE, related_name="events")
    date = models.DateField()
    event = models.OneToOneField(
        "core.Event", models.SET_NULL, null=True, related_name="planning_link"
    )
    original_event_id = models.PositiveIntegerField()

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["period", "date"], name="planning_unique_event_date")
        ]


class PlannedShift(models.Model):
    event = models.ForeignKey(PlannedEvent, models.CASCADE, related_name="shifts")
    shift = models.OneToOneField("core.Shift", models.SET_NULL, null=True)
    original_shift_id = models.PositiveIntegerField()
    snapshot = models.JSONField()


class SurveyResponse(models.Model):
    period = models.ForeignKey(PlanningPeriod, models.CASCADE, related_name="responses")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE)
    offered_shifts = models.ManyToManyField(PlannedShift)
    observer_events = models.ManyToManyField(
        PlannedEvent, blank=True, related_name="observer_responses"
    )
    maximum = models.PositiveIntegerField(_("Personal maximum"), null=True)
    notes = models.TextField(_("Notes"), max_length=4000, blank=True)
    submitted_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(null=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["period", "user"], name="planning_unique_response")
        ]

    def get_absolute_url(self):
        return reverse("ephios_shift_coordination:survey_detail", args=[self.period_id])


class Availability(models.Model):
    class Rating(models.TextChoices):
        UNAVAILABLE = "unavailable", _("Unavailable")
        IF_NEEDED = "if_needed", _("If needed")
        AVAILABLE = "available", _("Available")
        PREFERRED = "preferred", _("Preferred")

    response = models.ForeignKey(SurveyResponse, models.CASCADE, related_name="availabilities")
    planned_shift = models.ForeignKey(PlannedShift, models.CASCADE)
    rating = models.CharField(max_length=16, choices=Rating.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["response", "planned_shift"], name="planning_unique_availability"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    rating__in=["unavailable", "if_needed", "available", "preferred"]
                ),
                name="planning_valid_rating",
            ),
        ]


class NotificationDispatch(models.Model):
    period = models.ForeignKey(PlanningPeriod, models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE)
    kind = models.CharField(max_length=16)
    key = models.CharField(max_length=64)
    notification = models.OneToOneField("core.Notification", models.SET_NULL, null=True)
    skipped = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["period", "user", "kind", "key"], name="planning_unique_dispatch"
            )
        ]


class DraftAssignment(models.Model):
    period = models.ForeignKey(PlanningPeriod, models.CASCADE, related_name="draft_assignments")
    planned_shift = models.ForeignKey(PlannedShift, models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    original_user_id = models.PositiveIntegerField()
    observer = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["planned_shift", "original_user_id"], name="planning_unique_assignment"
            )
        ]


class ObserverParticipation(models.Model):
    """Native placeholder of a member sitting in: counted as staff, without working hours."""

    participation = models.OneToOneField(
        "core.PlaceholderParticipation", models.CASCADE, related_name="planning_observer"
    )
    planned_shift = models.ForeignKey(PlannedShift, models.CASCADE, related_name="observers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["planned_shift", "user"], name="planning_unique_observer"
            )
        ]


class RuleOverride(models.Model):
    period = models.ForeignKey(PlanningPeriod, models.CASCADE, related_name="rule_overrides")
    draft_version = models.PositiveIntegerField()
    code = models.CharField(max_length=32)
    token = models.CharField(max_length=64)
    facts = models.JSONField()
    reason = models.TextField(max_length=2000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class ReminderDispatch(models.Model):
    """One row per reminder that went out or was deliberately skipped, keyed by its moment."""

    shift = models.ForeignKey("core.Shift", models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE, related_name="+")
    key = models.CharField(max_length=64)
    notification = models.OneToOneField("core.Notification", models.SET_NULL, null=True)
    skipped = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shift", "user", "key"], name="planning_unique_reminder"
            )
        ]


def minutes_path(instance, filename):
    """A generated name: what somebody called the upload is not kept."""
    return f"assembly-minutes/{uuid.uuid4()}.pdf"


class Assembly(models.Model):
    """An assembly is a native event with one shift, plus an agenda and an invitation."""

    event = models.OneToOneField("core.Event", models.CASCADE, related_name="assembly")
    agenda = models.TextField(_("Agenda"), blank=True, max_length=8000)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    invited_at = models.DateTimeField(null=True)

    def get_absolute_url(self):
        return self.event.get_absolute_url()


class AssemblyMinutes(models.Model):
    """The minutes of one assembly, filed as PDF by the people responsible for it."""

    assembly = models.ForeignKey(Assembly, models.CASCADE, related_name="minutes")
    file = models.FileField(
        _("File"), upload_to=minutes_path, validators=[FileExtensionValidator(["pdf"])]
    )
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-pk"]


@receiver(models.signals.post_delete, sender=AssemblyMinutes)
def drop_minutes_file(sender, instance, using, **kwargs):
    """Removing the row has to take the blob with it, as ephios' own files plugin does."""
    transaction.on_commit(lambda: instance.file.delete(save=False), using)
