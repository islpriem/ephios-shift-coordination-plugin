import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays
from django import forms
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from ephios.core.models import EventType

from .dates import local_datetime, next_month
from .models import INSTANCE_SETTINGS, PlanningSettings, ServiceTemplate, ShiftTemplate

WEEKDAYS = list(
    enumerate(
        [
            _("Monday"),
            _("Tuesday"),
            _("Wednesday"),
            _("Thursday"),
            _("Friday"),
            _("Saturday"),
            _("Sunday"),
        ]
    )
)
INSTANCE_FIELDS = INSTANCE_SETTINGS
RULE_FIELDS = [
    field.name
    for field in PlanningSettings._meta.fields
    if field.name != "id" and field.name not in INSTANCE_FIELDS
]
# Values that only make sense for the whole instance, not per planning period.
GLOBAL_FIELDS = ("country", "region", "solver_seconds", "minimum_regular")

REMINDER_HELP = {
    "service_reminder_days": _(
        "Everybody staffed for a service, including anybody sitting in, is reminded this many "
        "days before it starts. Separate several values with commas, 0 means the same day. "
        "Empty means no reminder."
    ),
    "assembly_reminder_days": _(
        "Everybody invited to an assembly is reminded this many days before it starts, "
        "whatever they answered. Separate several values with commas, 0 means the same day. "
        "Empty means no reminder."
    ),
}

SETTINGS_HELP = {
    "next_period_weeks": _(
        "Suggested day for the reminder about the next planning period, counted back from "
        "the end of the one being created."
    ),
    "weekdays": _("Preselected weekdays for new planning periods."),
    "country": _("Country of the holiday calendar."),
    "region": _("Region of the holiday calendar. Without a region, holidays cannot be skipped."),
    "exclude_holidays": _("Preselection for new planning periods: skip public holidays."),
    "response_days": _("Default time to answer, counted from opening a survey."),
    "reminder_days": _(
        "Members without a complete answer are reminded this many days before the deadline. "
        "Separate several values with commas, for example 3, 1. Empty means no reminder."
    ),
    "weekly_limit": _(
        "Most shifts one person takes per calendar week (Monday to Sunday) within one event type."
    ),
    "free_next_day": _("Keep the day after a service free for that person."),
    "solver_seconds": _(
        "Time budget for one automatic proposal. A higher value can improve a proposal, but the "
        "request takes longer; application and proxy timeouts have to be above it."
    ),
    "allow_observers": _(
        "Preselection for new planning periods: members may offer to sit in on a service. "
        "Sitting in needs no qualification, counts as staffing and earns no working hours."
    ),
    "minimum_regular": _(
        "Regularly staffed people a shift needs at least once somebody sits in. This prevents "
        "shifts that are staffed by people sitting in only."
    ),
}

PERIOD_HELP = {
    "weekdays": _("Services are suggested for these weekdays. You pick the single dates below."),
    "exclude_holidays": _("Skip the public holidays of the configured region."),
    "response_days": _("Used for the response deadline suggested when you open the survey."),
    "reminder_days": _(
        "Suggested reminders when you open the survey. Separate several values with commas."
    ),
    "weekly_limit": _("Most shifts per person and calendar week (Monday to Sunday)."),
    "free_next_day": _("Nobody is planned on two days in a row."),
    "allow_observers": _(
        "Members may offer to sit in on a service of this period: no qualification required, "
        "counted as staffing, no working hours."
    ),
}


def offsets(value):
    """Read a comma separated list of day offsets, as every reminder field uses one."""
    try:
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise forms.ValidationError(_("Enter whole numbers separated by commas.")) from exc


class RuleForm(forms.ModelForm):
    help_texts = SETTINGS_HELP
    weekdays = forms.TypedMultipleChoiceField(
        label=_("Weekdays"), choices=WEEKDAYS, coerce=int, widget=forms.CheckboxSelectMultiple
    )
    reminder_days = forms.CharField(
        label=_("Reminders in days before the deadline"), required=False
    )
    country = forms.ChoiceField(
        label=_("Holiday country"),
        choices=[(code, code) for code in sorted(holidays.list_supported_countries())],
    )
    region = forms.ChoiceField(label=_("Holiday region"), required=False)

    class Meta:
        model = PlanningSettings
        fields = RULE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        country = self.data.get("country") if self.is_bound else self.initial.get("country", "DE")
        self.fields["region"].choices = [
            ("", _("Select a region")),
            *[(code, code) for code in holidays.list_supported_countries().get(country, [])],
        ]
        offsets = self.initial.get("reminder_days", [3])
        self.initial["reminder_days"] = ", ".join(map(str, offsets))
        for name, help_text in self.help_texts.items():
            if name in self.fields:
                self.fields[name].help_text = help_text

    def clean_reminder_days(self):
        return offsets(self.cleaned_data["reminder_days"])


class SettingsForm(RuleForm):
    planning_groups = forms.ModelMultipleChoiceField(
        label=_("Groups allowed to manage planning"),
        queryset=Group.objects.all(),
        required=False,
        help_text=_(
            "Members of these groups create planning periods, open surveys and publish plans. "
            "In ephios they also need the rights to create events and to publish them for the "
            "template's groups."
        ),
    )
    service_reminder_days = forms.CharField(
        label=_("Remind about services this many days before"), required=False
    )
    assembly_reminder_days = forms.CharField(
        label=_("Remind about assemblies this many days before"), required=False
    )

    api_event_types = forms.ModelMultipleChoiceField(
        label=_("Event types in the public duty information"),
        queryset=EventType.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Only shifts of these types are counted, and only while they are staffed."),
    )

    class Meta(RuleForm.Meta):
        fields = [*RULE_FIELDS, *INSTANCE_FIELDS]
        widgets = {
            name: forms.TimeInput(format="%H:%M", attrs={"type": "time"})
            for name in ("service_reminder_time", "assembly_reminder_time")
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["planning_groups"] = Group.objects.filter(
            permissions__codename="manage_planning",
            permissions__content_type__app_label="ephios_shift_coordination",
        )
        for name in ("service_reminder_days", "assembly_reminder_days"):
            self.initial[name] = ", ".join(map(str, self.initial.get(name) or []))
            self.fields[name].help_text = REMINDER_HELP[name]
        self.initial["api_event_types"] = self.instance.api_event_types.all()
        self.fields["api_enabled"].help_text = _(
            "Answers three questions without a login: whether a duty runs now, when the next "
            "one starts and which weekdays of this week carry one. No personal data is shared."
        )

    def clean_service_reminder_days(self):
        return offsets(self.cleaned_data["service_reminder_days"])

    def clean_assembly_reminder_days(self):
        return offsets(self.cleaned_data["assembly_reminder_days"])

    @transaction.atomic
    def save(self):
        instance = super().save()
        instance.api_event_types.set(self.cleaned_data["api_event_types"])
        permission = Permission.objects.get(
            content_type__app_label="ephios_shift_coordination", codename="manage_planning"
        )
        desired = self.cleaned_data["planning_groups"]
        for group in Group.objects.filter(permissions=permission).exclude(pk__in=desired):
            group.permissions.remove(permission)
        for group in desired:
            group.permissions.add(permission)
        return instance


class ServiceTemplateForm(forms.ModelForm):
    class Meta:
        model = ServiceTemplate
        fields = [
            "title",
            "description",
            "location",
            "event_type",
            "visible_for",
            "responsible_groups",
            "responsible_users",
        ]
        help_texts = {
            "title": _("Title of every event created from this template."),
            "description": _("Shown on the event page in ephios."),
            "event_type": _(
                "Weekly limits, overlaps and consecutive days only count services of this type."
            ),
            "visible_for": _("These groups see the services and are invited to the survey."),
            "responsible_groups": _(
                "May edit the created events and receive messages about missing staff. "
                "Coordinators need this to plan the period."
            ),
            "responsible_users": _("Additional responsible people besides the groups."),
        }


class ShiftTemplateForm(forms.ModelForm):
    class Meta:
        model = ShiftTemplate
        fields = [
            "label",
            "meeting_time",
            "start_time",
            "end_time",
            "end_day_offset",
            "minimum",
            "maximum",
            "qualifications",
        ]
        widgets = {
            name: forms.TimeInput(format="%H:%M", attrs={"type": "time"})
            for name in ("meeting_time", "start_time", "end_time")
        }
        help_texts = {
            "label": _("Short name, for example phone or early shift."),
            "meeting_time": _("When people meet, at the latest at the start."),
            "end_day_offset": _("Choose the next day for shifts that run past midnight."),
            "minimum": _("Automatic planning staffs exactly this many people, or nobody."),
            "maximum": _("Empty means no limit. More people only through a manual exception."),
            "qualifications": _("Only members holding all of these are asked for this shift."),
        }


class BaseShiftFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        identifiers = []
        for form in self.forms:
            shift = form.cleaned_data.get("id")
            if shift:
                if shift.template_id != self.instance.pk or shift.pk in identifiers:
                    raise forms.ValidationError(
                        _("Each shift must belong to this template and appear only once.")
                    )
                identifiers.append(shift.pk)


ShiftFormSet = inlineformset_factory(
    ServiceTemplate,
    ShiftTemplate,
    form=ShiftTemplateForm,
    formset=BaseShiftFormSet,
    extra=2,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class PeriodForm(RuleForm):
    help_texts = PERIOD_HELP
    template = forms.ModelChoiceField(
        label=_("Service template"),
        queryset=ServiceTemplate.objects.all(),
        help_text=_(
            "Sets title, location, visibility and the shifts of every service in this period."
        ),
    )
    start_date = forms.DateField(
        label=_("First day"), widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    end_date = forms.DateField(
        label=_("Last day"), widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    creation_key = forms.UUIDField(widget=forms.HiddenInput)
    remind_next = forms.BooleanField(
        label=_("Remind me to plan the next period"),
        required=False,
        help_text=_("You get one message on that day, with a link to a new planning period."),
    )
    next_reminder_date = forms.DateField(
        label=_("Remind me on"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    class Meta(RuleForm.Meta):
        fields = [name for name in RULE_FIELDS if name not in GLOBAL_FIELDS]

    def __init__(self, *args, defaults, reminder_weeks, **kwargs):
        super().__init__(*args, **kwargs)
        for name in GLOBAL_FIELDS:
            self.fields.pop(name, None)
            setattr(self.instance, name, defaults[name])
        start, end = next_month(timezone.localdate())
        self.initial.update(start_date=start, end_date=end, creation_key=uuid.uuid4())
        if not defaults["region"]:
            self.fields["exclude_holidays"].disabled = True
            self.fields["exclude_holidays"].help_text = _(
                "Configure a holiday region in the planning settings to use this."
            )
        self.reminder_weeks = reminder_weeks
        if self.is_bound:
            # The reminder step is only rendered after the dates, so on the way there the
            # form answers for itself: reminder on, at the suggested day.
            self.data = self.data.copy()
            if "reminder_ready" not in self.data:
                self.data["remind_next"] = "on"
                self.data["next_reminder_date"] = ""
            if self.data.get("remind_next") and not self.data.get("next_reminder_date"):
                self.data["next_reminder_date"] = self.suggested_reminder()

    def suggested_reminder(self):
        """Some weeks before this period ends, so there is time to plan the next one."""
        try:
            end = date.fromisoformat(self.data.get("end_date", ""))
        except ValueError:
            return ""
        return (end - timedelta(weeks=self.reminder_weeks)).isoformat()

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("start_date")
            and cleaned.get("end_date")
            and cleaned["start_date"] > cleaned["end_date"]
        ):
            self.add_error("end_date", _("The last day must not precede the first day."))
        if cleaned.get("remind_next"):
            day = cleaned.get("next_reminder_date")
            if not day:
                self.add_error("next_reminder_date", _("Choose the day of the reminder."))
            elif day <= timezone.localdate():
                self.add_error("next_reminder_date", _("The reminder has to be in the future."))
            elif cleaned.get("end_date") and day > cleaned["end_date"]:
                self.add_error(
                    "next_reminder_date",
                    _("A reminder after the period has ended comes too late to be useful."),
                )
        return cleaned

    def reminder_day(self):
        return self.cleaned_data["next_reminder_date"] if self.cleaned_data["remind_next"] else None

    def rules(self):
        return {
            name: self.cleaned_data[name]
            if name in self.cleaned_data
            else getattr(self.instance, name)
            for name in RULE_FIELDS
        }

    def selected_dates(self, values):
        try:
            dates = sorted(date.fromisoformat(value) for value in values)
        except ValueError as exc:
            raise forms.ValidationError(_("Invalid selected date.")) from exc
        if (
            not dates
            or len(set(dates)) != len(dates)
            or any(
                not self.cleaned_data["start_date"] <= day <= self.cleaned_data["end_date"]
                for day in dates
            )
        ):
            raise forms.ValidationError(_("Select distinct dates within the period."))
        return dates


class SurveyOpeningForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    deadline = forms.DateTimeField(
        label=_("Response deadline"),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text=_(
            "Until then members can answer and change their answer. It has to be before the "
            "first shift starts."
        ),
    )
    reminder_days = forms.CharField(
        label=_("Reminders in days before the deadline"),
        required=False,
        help_text=_(
            "Members without a complete answer get a reminder. Separate several values with "
            "commas, for example 3, 1. Empty means no reminder."
        ),
    )
    maximum = forms.IntegerField(
        label=_("Recommended shifts per person"),
        min_value=0,
        required=False,
        help_text=_("Shown to members as a suggestion for their personal maximum."),
    )
    clean_reminder_days = RuleForm.clean_reminder_days

    def __init__(self, *args, hint="", **kwargs):
        super().__init__(*args, **kwargs)
        if hint:
            self.fields["maximum"].help_text = hint


class SurveyClosingForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    confirm_close = forms.BooleanField(
        label=_("Close the survey now. Answers become read-only, planning continues.")
    )


class SurveyResponseForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    maximum = forms.IntegerField(
        label=_("Your personal maximum"),
        min_value=0,
        max_value=2147483647,
        help_text=_(
            "How many shifts may we assign to you at most in this period? Enter 0 if you cannot "
            "take a shift this time."
        ),
    )
    notes = forms.CharField(
        label=_("Notes for the coordinators"),
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_(
            "Optional, for example holidays or who you would like to work with. Only "
            "coordinators read this."
        ),
    )

    def __init__(self, *args, response, **kwargs):
        from .ephios_integration import eligible
        from .models import Availability

        super().__init__(*args, **kwargs)
        self.response = response
        period = response.period
        self.scale = Availability.Rating.choices
        self.allow_observers = bool(period.rules.get("allow_observers"))
        self.initial.update(
            expected_version=response.version,
            maximum=response.maximum
            if response.maximum is not None
            else period.suggestion.get("maximum"),
            notes=response.notes,
        )
        zone = ZoneInfo(period.timezone)
        ratings = dict(response.availabilities.values_list("planned_shift_id", "rating"))
        self.offered = sorted(
            response.offered_shifts.select_related("shift__event__type", "event"),
            key=lambda shift: (datetime.fromisoformat(shift.snapshot["start_time"]), shift.pk),
        )
        columns, by_event = {}, defaultdict(dict)
        for shift in self.offered:
            start = datetime.fromisoformat(shift.snapshot["start_time"]).astimezone(zone)
            end = datetime.fromisoformat(shift.snapshot["end_time"]).astimezone(zone)
            column = f"{shift.snapshot['label']} {start:%H:%M}"
            hours = "{start} – {end}".format(
                start=date_format(start, "TIME_FORMAT"), end=date_format(end, "TIME_FORMAT")
            )
            columns.setdefault(
                column,
                {
                    "key": column,
                    "label": shift.snapshot["label"],
                    "time": hours,
                    "order": start.time(),
                },
            )
            by_event[shift.event_id][column] = shift
            name = f"rating_{shift.pk}"
            self.fields[name] = forms.ChoiceField(
                label=_("{shift} on {date}").format(
                    shift=shift.snapshot["label"], date=date_format(start, "DATE_FORMAT")
                ),
                choices=Availability.Rating.choices,
                widget=forms.RadioSelect,
                help_text=""
                if eligible(response.user, shift)
                else _("You currently do not meet the requirements for this shift."),
            )
            self.initial[name] = ratings.get(shift.pk)
        self.columns = sorted(columns.values(), key=lambda column: (column["order"], column["key"]))
        wished = set(response.observer_events.values_list("pk", flat=True))
        self.rows, self.observed = [], []
        for link in period.events.select_related("event").order_by("date"):
            observable = (
                self.allow_observers
                and link.event is not None
                and link.event.active
                and response.user.has_perm("core.view_event", link.event)
            )
            if not by_event[link.pk] and not observable:
                continue
            observe = None
            if observable:
                observe = f"observe_{link.pk}"
                self.fields[observe] = forms.BooleanField(
                    label=_("Sit in on the service on {date}").format(
                        date=date_format(link.date, "DATE_FORMAT")
                    ),
                    required=False,
                )
                self.initial[observe] = link.pk in wished
                self.observed.append((link.pk, observe))
            self.rows.append(
                {
                    "date": link.date,
                    "cells": [
                        {
                            "column": column["key"],
                            "name": shift and f"rating_{shift.pk}",
                        }
                        for column in self.columns
                        for shift in [by_event[link.pk].get(column["key"])]
                    ],
                    "observe": observe,
                }
            )

    def table(self):
        for row in self.rows:
            yield {
                "date": row["date"],
                "cells": [
                    {
                        "column": cell["column"],
                        "field": self[cell["name"]] if cell["name"] else None,
                        "warning": self.fields[cell["name"]].help_text if cell["name"] else "",
                    }
                    for cell in row["cells"]
                ],
                "observe": self[row["observe"]] if row["observe"] else None,
            }

    def clean(self):
        cleaned = super().clean()
        supplied = {key for key in self.data if key.startswith("rating_")}
        expected = {f"rating_{shift.pk}" for shift in self.offered}
        if supplied != expected or any(len(self.data.getlist(key)) != 1 for key in supplied):
            raise forms.ValidationError(_("Rate exactly the originally offered shifts."))
        return cleaned

    def ratings(self):
        return {shift.pk: self.cleaned_data[f"rating_{shift.pk}"] for shift in self.offered}

    def observer_events(self):
        return [pk for pk, name in self.observed if self.cleaned_data.get(name)]


class PublicationForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    fingerprint = forms.RegexField(regex=r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    confirmed_tokens = forms.MultipleChoiceField(
        label=_("Confirm every recorded exception once more"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    confirm_underfilled = forms.BooleanField(
        label=_("I publish although some shifts are missing people."),
        required=False,
    )
    confirm_publish = forms.BooleanField(
        label=_("I checked this plan and want to publish it. Everybody staffed gets a message.")
    )

    def __init__(self, *args, review, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["confirmed_tokens"].choices = [
            (record.token, f"{record.rule_label} · {record.people_label} · {record.shifts_label}")
            for record in review["overrides"]
        ]
        self.fields["confirm_underfilled"].required = bool(review["underfilled"])
        self.initial.update(expected_version=review["version"], fingerprint=review["fingerprint"])


class StaffingForm(forms.Form):
    user_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        choices=[("join", _("Sign up")), ("observe", _("Sit in")), ("leave", _("Sign off"))],
        widget=forms.HiddenInput,
    )


class AssemblyForm(forms.Form):
    """Everything needed to call an assembly; the kind of assembly supplies the rest."""

    event_type = forms.ModelChoiceField(
        label=_("Kind of assembly"),
        queryset=EventType.objects.none(),
        empty_label=None,
        help_text=_("Who is invited and who is responsible comes from this kind."),
    )
    title = forms.CharField(label=_("Title"), max_length=254)
    location = forms.CharField(label=_("Location"), max_length=254)
    description = forms.CharField(
        label=_("Description"), widget=forms.Textarea(attrs={"rows": 3}), required=False
    )
    date = forms.DateField(
        label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    start = forms.TimeField(
        label=_("Start"), widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"})
    )
    end = forms.TimeField(
        label=_("End"), widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"})
    )
    agenda = forms.CharField(
        label=_("Agenda"),
        widget=forms.Textarea(attrs={"rows": 6}),
        required=False,
        max_length=8000,
        help_text=_("One item per line. It is shown at the assembly and sent with the invitation."),
    )
    silent = forms.BooleanField(
        label=_("Call quietly, send the invitation later"),
        required=False,
        help_text=_("Nobody is notified now. You can send the invitation from the assembly page."),
    )

    def __init__(self, *args, types, defaults, **kwargs):
        """Start from the first kind of assembly; picking another one is one click away."""
        super().__init__(*args, initial={"event_type": types[0], **defaults}, **kwargs)
        self.fields["event_type"].queryset = EventType.objects.filter(
            pk__in=[event_type.pk for event_type in types]
        )

    def clean(self):
        data = super().clean()
        if not (data.get("date") and data.get("start") and data.get("end")):
            return data
        data["start_at"] = local_datetime(data["date"], data["start"], settings.TIME_ZONE)
        data["end_at"] = local_datetime(data["date"], data["end"], settings.TIME_ZONE)
        if data["end_at"] <= data["start_at"]:
            raise forms.ValidationError(_("The assembly has to end after it starts."))
        if data["start_at"] <= timezone.now():
            raise forms.ValidationError(_("An assembly has to start in the future."))
        return data

    def assembly_arguments(self):
        data = self.cleaned_data
        return {
            "event_type": data["event_type"],
            "title": data["title"],
            "description": data["description"],
            "location": data["location"],
            "start": data["start_at"],
            "end": data["end_at"],
            "agenda": data["agenda"],
            "silent": data["silent"],
        }


class MinutesForm(forms.Form):
    """One PDF per upload; the file name is replaced by a generated one."""

    file = forms.FileField(
        label=_("Assembly minutes as PDF"),
        validators=[FileExtensionValidator(["pdf"])],
        widget=forms.FileInput(attrs={"accept": ".pdf"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        _used, free = settings.GET_USERCONTENT_QUOTA()
        if upload.size > free:
            raise forms.ValidationError(
                _("The file is too large. There are only %(quota)s available."),
                params={"quota": filesizeformat(free)},
            )
        return upload
