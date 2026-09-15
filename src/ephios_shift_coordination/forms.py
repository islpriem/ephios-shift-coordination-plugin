import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import holidays
from django import forms
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from .dates import next_month
from .models import PlanningSettings, ServiceTemplate, ShiftTemplate

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
RULE_FIELDS = [field.name for field in PlanningSettings._meta.fields if field.name != "id"]


class RuleForm(forms.ModelForm):
    weekdays = forms.TypedMultipleChoiceField(
        label=_("Weekdays"), choices=WEEKDAYS, coerce=int, widget=forms.CheckboxSelectMultiple
    )
    reminder_days = forms.CharField(
        label=_("Reminder days before deadline"),
        required=False,
        help_text=_("Comma-separated positive day offsets; leave empty for no reminders."),
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

    def clean_reminder_days(self):
        try:
            return [
                int(value.strip())
                for value in self.cleaned_data["reminder_days"].split(",")
                if value.strip()
            ]
        except ValueError as exc:
            raise forms.ValidationError(_("Enter whole numbers separated by commas.")) from exc

    def rules(self):
        return {name: self.cleaned_data[name] for name in RULE_FIELDS}


class SettingsForm(RuleForm):
    planning_groups = forms.ModelMultipleChoiceField(
        label=_("Groups allowed to manage planning"), queryset=Group.objects.all(), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["planning_groups"] = Group.objects.filter(
            permissions__codename="manage_planning",
            permissions__content_type__app_label="ephios_shift_coordination",
        )

    @transaction.atomic
    def save(self):
        instance = super().save()
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
    template = forms.ModelChoiceField(
        label=_("Service template"), queryset=ServiceTemplate.objects.all()
    )
    start_date = forms.DateField(
        label=_("Start date"), widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    end_date = forms.DateField(
        label=_("End date"), widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    creation_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        start, end = next_month(timezone.localdate())
        self.initial.update(start_date=start, end_date=end, creation_key=uuid.uuid4())

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("start_date")
            and cleaned.get("end_date")
            and cleaned["start_date"] > cleaned["end_date"]
        ):
            self.add_error("end_date", _("The end date must not precede the start date."))
        return cleaned

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
    )
    reminder_days = forms.CharField(
        label=_("Reminder days before deadline"),
        required=False,
        help_text=_("Comma-separated positive day offsets; leave empty for no reminders."),
    )
    clean_reminder_days = RuleForm.clean_reminder_days


class SurveyResponseForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    maximum = forms.IntegerField(label=_("Personal maximum"), min_value=0, max_value=2147483647)
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=4000, widget=forms.Textarea
    )

    def __init__(self, *args, response, **kwargs):
        from .ephios_integration import eligible
        from .models import Availability

        super().__init__(*args, **kwargs)
        self.response = response
        self.initial.update(
            expected_version=response.version, maximum=response.maximum, notes=response.notes
        )
        ratings = dict(response.availabilities.values_list("planned_shift_id", "rating"))
        self.offered = sorted(
            response.offered_shifts.select_related("shift__event__type", "event"),
            key=lambda shift: (datetime.fromisoformat(shift.snapshot["start_time"]), shift.pk),
        )
        for shift in self.offered:
            name = f"rating_{shift.pk}"
            start = datetime.fromisoformat(shift.snapshot["start_time"]).astimezone(
                ZoneInfo(response.period.timezone)
            )
            end = datetime.fromisoformat(shift.snapshot["end_time"]).astimezone(
                ZoneInfo(response.period.timezone)
            )
            label = (
                f"{date_format(start, 'DATETIME_FORMAT')} – {date_format(end, 'TIME_FORMAT')}"
                f" · {shift.snapshot['label']}"
            )
            self.fields[name] = forms.ChoiceField(
                label=label,
                choices=Availability.Rating.choices,
                widget=forms.RadioSelect,
                help_text=_("Currently no longer eligible for this shift.")
                if not eligible(response.user, shift)
                else "",
            )
            self.initial[name] = ratings.get(shift.pk)

    def clean(self):
        cleaned = super().clean()
        supplied = {key for key in self.data if key.startswith("rating_")}
        expected = {f"rating_{shift.pk}" for shift in self.offered}
        if supplied != expected or any(len(self.data.getlist(key)) != 1 for key in supplied):
            raise forms.ValidationError(_("Rate exactly the originally offered shifts."))
        return cleaned

    def ratings(self):
        return {shift.pk: self.cleaned_data[f"rating_{shift.pk}"] for shift in self.offered}


class PublicationForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    fingerprint = forms.RegexField(regex=r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    confirmed_tokens = forms.MultipleChoiceField(
        label=_("Confirm every recorded exception for publication"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    confirm_underfilled = forms.BooleanField(
        label=_("I confirm publication with the missing minimum places shown above."),
        required=False,
    )
    confirm_publish = forms.BooleanField(
        label=_("I confirm publication of this saved shared draft.")
    )

    def __init__(self, *args, review, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["confirmed_tokens"].choices = [
            (record.token, f"{record.rule_label} · {record.people_label} · {record.shifts_label}")
            for record in review["overrides"]
        ]
        self.fields["confirm_underfilled"].required = bool(review["underfilled"])
        self.initial.update(expected_version=review["version"], fingerprint=review["fingerprint"])
