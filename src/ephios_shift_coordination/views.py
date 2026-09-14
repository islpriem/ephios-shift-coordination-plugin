import json
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .access import require_access
from .dates import calendar_days
from .drafts import RULE_LABELS, load_plan, save_draft, validate_draft
from .ephios_integration import eligible
from .forms import (
    PeriodForm,
    ServiceTemplateForm,
    SettingsForm,
    ShiftFormSet,
    SurveyOpeningForm,
    SurveyResponseForm,
)
from .models import PlanningPeriod, PlanningSettings, ServiceTemplate, SurveyResponse
from .services import Conflict, create_period, preview_template
from .surveys import open_survey, save_response


@require_access()
@require_http_methods(["GET"])
def plan(request, pk):
    period = get_object_or_404(PlanningPeriod, pk=pk)
    context = {"period": period}
    status = 200
    try:
        context["planning_data"] = load_plan(request.user, pk)
        history = list(period.rule_overrides.select_related("actor"))
        people = {p["id"]: p["name"] for p in context["planning_data"]["people"]}
        shifts = {s["id"]: s for s in context["planning_data"]["shifts"]}
        for record in history:
            record.rule_label = RULE_LABELS[record.code]
            record.people_label = ", ".join(
                people.get(pk, str(pk)) for pk in record.facts["person_ids"]
            )
            record.shifts_label = ", ".join(
                f"{shifts[pk]['date']} {shifts[pk]['label']}"
                for pk in record.facts["shift_ids"]
                if pk in shifts
            )
        context["history"] = history
    except Conflict as exc:
        context["error"] = str(exc)
        status = 409
    return render(request, "ephios_shift_coordination/plan.html", context, status=status)


def draft_request(request, pk, *, save=False):
    try:
        payload = json.loads(request.body)
        expected = {"expected_version", "fingerprint", "assignments"}
        if save:
            expected.add("confirmations")
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValidationError(_("Invalid draft request."))
        result = (save_draft if save else validate_draft)(request.user, pk, **payload)
        return JsonResponse(result)
    except json.JSONDecodeError, UnicodeDecodeError:
        return JsonResponse({"error": _("Invalid draft request.")}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    except Conflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)


@require_access()
@require_http_methods(["POST"])
def draft_validate(request, pk):
    return draft_request(request, pk)


@require_access()
@require_http_methods(["POST"])
def draft_save(request, pk):
    return draft_request(request, pk, save=True)


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
    return render_period(request, period)


def render_period(request, period, form=None, status=200):
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
    if form is None:
        form = SurveyOpeningForm(
            initial={
                "expected_version": period.version,
                "deadline": period.deadline
                or timezone.now() + timedelta(days=period.rules["response_days"]),
                "reminder_days": ", ".join(map(str, period.rules["reminder_days"])),
            }
        )
    responses = []
    for response in period.responses.select_related("user").prefetch_related(
        "availabilities__planned_shift"
    ):
        response.eligibility_warnings = [
            shift.snapshot["label"]
            for shift in response.offered_shifts.select_related("shift__event__type")
            if not eligible(response.user, shift)
        ]
        responses.append(response)
    return render(
        request,
        "ephios_shift_coordination/period_detail.html",
        {"period": period, "events": events, "form": form, "responses": responses},
        status=status,
    )


@require_access("member")
@require_http_methods(["GET"])
def survey_list(request):
    return render(
        request,
        "ephios_shift_coordination/survey_list.html",
        {
            "responses": SurveyResponse.objects.filter(user=request.user)
            .select_related("period")
            .order_by("-period__start_date", "-pk"),
        },
    )


@require_access()
@require_http_methods(["POST"])
def survey_open(request, pk):
    period = get_object_or_404(PlanningPeriod, pk=pk)
    form = SurveyOpeningForm(request.POST)
    status = 200
    if form.is_valid():
        try:
            open_survey(
                request.user,
                pk,
                **form.cleaned_data,
                open_now=request.POST.get("action") != "save_settings",
            )
            return redirect(period)
        except (Conflict, ValidationError) as exc:
            form.add_error(
                None, str(exc) if isinstance(exc, Conflict) else ValidationError(exc.messages)
            )
            status = 409 if isinstance(exc, Conflict) else 200
    return render_period(request, period, form, status)


@require_access("member")
@require_http_methods(["GET", "POST"])
def survey_detail(request, pk):
    response = get_object_or_404(
        SurveyResponse.objects.select_related("period", "user"), period_id=pk, user=request.user
    )
    form = SurveyResponseForm(request.POST if request.method == "POST" else None, response=response)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            save_response(
                request.user,
                pk,
                **{
                    "expected_version": form.cleaned_data["expected_version"],
                    "maximum": form.cleaned_data["maximum"],
                    "notes": form.cleaned_data["notes"],
                    "ratings": form.ratings(),
                },
            )
            return redirect(response)
        except (Conflict, ValidationError) as exc:
            form.add_error(
                None, str(exc) if isinstance(exc, Conflict) else ValidationError(exc.messages)
            )
            status = 409 if isinstance(exc, Conflict) else 200
    writable = response.period.effective_state == PlanningPeriod.State.SURVEY_OPEN
    if writable and any(
        shift.shift is None or not request.user.has_perm("core.view_event", shift.shift.event)
        for shift in form.offered
    ):
        writable = False
    if not writable:
        for field in form.fields.values():
            field.disabled = True
    return render(
        request,
        "ephios_shift_coordination/survey_detail.html",
        {"response": response, "form": form, "writable": writable},
        status=status,
    )
