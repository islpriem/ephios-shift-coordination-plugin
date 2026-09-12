from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .access import require_access
from .dates import calendar_days
from .forms import PeriodForm, ServiceTemplateForm, SettingsForm, ShiftFormSet
from .models import PlanningPeriod, PlanningSettings, ServiceTemplate
from .services import Conflict, create_period, preview_template


@require_access("admin")
@require_http_methods(["GET", "POST"])
def configuration(request):
    instance, _created = PlanningSettings.objects.get_or_create(pk=1)
    form = SettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ephios_shift_coordination:settings")
    return render(request, "ephios_shift_coordination/settings.html", {"form": form})


@require_access("admin")
@require_http_methods(["GET"])
def template_list(request):
    return render(
        request,
        "ephios_shift_coordination/template_list.html",
        {"templates": ServiceTemplate.objects.prefetch_related("shifts")},
    )


@require_access("admin")
@require_http_methods(["GET", "POST"])
@transaction.atomic
def template_edit(request, pk=None):
    template = (
        get_object_or_404(ServiceTemplate.objects.select_for_update(), pk=pk)
        if pk
        else ServiceTemplate()
    )
    form = ServiceTemplateForm(request.POST or None, instance=template)
    shifts = ShiftFormSet(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid() and shifts.is_valid():
        form.save()
        for position, shift_form in enumerate(shifts.forms):
            shift_form.instance.position = position
        shifts.save()
        return redirect("ephios_shift_coordination:template_list")
    return render(
        request, "ephios_shift_coordination/template_form.html", {"form": form, "shifts": shifts}
    )


@require_access()
@require_http_methods(["GET"])
def period_list(request):
    return render(
        request,
        "ephios_shift_coordination/period_list.html",
        {"periods": PlanningPeriod.objects.all()},
    )


@require_access()
@require_http_methods(["GET", "POST"])
def period_create(request):
    configuration, _created = PlanningSettings.objects.get_or_create(pk=1)
    form = PeriodForm(request.POST or None, initial=configuration.snapshot())
    context = {"form": form}
    status = 200
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        try:
            days = calendar_days(
                values["start_date"],
                values["end_date"],
                values["weekdays"],
                values["country"],
                values["region"],
                values["exclude_holidays"],
            )
            if request.POST.get("selection_ready") and request.POST.get("action") != "calendar":
                selected = form.selected_dates(request.POST.getlist("dates"))
            else:
                selected = [day["date"] for day in days if day["selected"]]
            for day in days:
                day["selected"] = day["date"] in selected
                day["column"] = day["date"].weekday() + 1
            context.update(days=days, selected_dates=selected)
            if request.POST.get("action") == "create":
                if not request.POST.get("selection_ready"):
                    raise ValidationError(_("Preview and select the dates before creating events."))
                period = create_period(
                    request.user,
                    template_id=values["template"].pk,
                    start_date=values["start_date"],
                    end_date=values["end_date"],
                    dates=selected,
                    rules=form.rules(),
                    creation_key=values["creation_key"],
                )
                return redirect(period)
            context["preview"] = preview_template(values["template"], selected, settings.TIME_ZONE)
        except ValidationError as exc:
            form.add_error(None, ValidationError(exc.messages))
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
    return render(request, "ephios_shift_coordination/period_form.html", context, status=status)


@require_access()
@require_http_methods(["GET"])
def period_detail(request, pk):
    period = get_object_or_404(
        PlanningPeriod.objects.prefetch_related("events__event__shifts"), pk=pk
    )
    events = [
        {
            "date": link.date,
            "missing": link.event is None,
            "event": link.event
            if link.event and request.user.has_perm("core.view_event", link.event)
            else None,
        }
        for link in period.events.all()
    ]
    return render(
        request,
        "ephios_shift_coordination/period_detail.html",
        {"period": period, "events": events},
    )


@require_access("member")
@require_http_methods(["GET"])
def survey_list(request):
    return render(request, "ephios_shift_coordination/survey_list.html")
