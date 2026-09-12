import uuid
from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .dates import holiday_calendar


def default_weekdays():
    return [1, 2, 3, 6]


def default_reminders():
    return [3]


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

    def snapshot(self):
        return {
            field.name: getattr(self, field.name)
            for field in self._meta.fields
            if field.name != "id"
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
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
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
