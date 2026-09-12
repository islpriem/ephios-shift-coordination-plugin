import uuid
from datetime import date

import holidays
from django import forms
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone
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
