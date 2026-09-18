import json
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods

from .access import enabled, require_access
from .assemblies import (
    answer,
    answer_options,
    answer_state,
    callable_types,
    invite,
    listed,
    listed_types,
    plan_assembly,
    resolve_answer,
    shift_of,
    would_invite,
)
from .dates import calendar_days
from .drafts import RULE_LABELS, load_plan, save_draft, validate_draft
from .ephios_integration import assembly_defaults, eligibility, period_shifts
from .forms import (
    AssemblyForm,
    MinutesForm,
    PeriodForm,
    ServiceTemplateForm,
    SettingsForm,
    ShiftFormSet,
    StaffingForm,
    SurveyClosingForm,
    SurveyOpeningForm,
    SurveyResponseForm,
)
from .minutes import display_name, file_minutes, remove
from .models import (
    Assembly,
    AssemblyMinutes,
    Availability,
    PlannedShift,
    PlanningPeriod,
    PlanningSettings,
    ServiceTemplate,
    SurveyResponse,
)
from .proposals import create_proposal
from .services import Conflict, create_period, preview_template
from .staffing import (
    add_person,
    options,
    remove_person,
    replacement_access,
    replacements,
    service,
    service_shifts,
)
from .surveys import capacity_preview, close_survey, open_survey, recommendation, save_response


@require_access()
@require_http_methods(["GET"])
def plan(request, pk):
    period = get_object_or_404(PlanningPeriod, pk=pk)
    if period.state == PlanningPeriod.State.PUBLISHED:
        return redirect("ephios_shift_coordination:publication", pk=pk)
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
        if not isinstance(payload, dict) or not expected <= set(payload) <= expected | {
            "observers"
        }:
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


@transaction.non_atomic_requests
@require_access()
@require_http_methods(["POST"])
def plan_propose(request, pk):
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict) or set(payload) != {"expected_version", "fingerprint"}:
            raise ValidationError(_("Invalid proposal request."))
        return JsonResponse(create_proposal(request.user, pk, **payload))
    except json.JSONDecodeError, UnicodeDecodeError, ValidationError:
        return JsonResponse({"error": _("Invalid proposal request.")}, status=400)
    except Conflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)


@require_access("admin")
@require_http_methods(["GET", "POST"])
def configuration(request):
    instance, _created = PlanningSettings.objects.get_or_create(pk=1)
    form = SettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("The planning settings have been saved."))
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
        messages.success(request, _("The service template has been saved."))
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
        {
            "periods": PlanningPeriod.objects.annotate(
                invited=Count("responses", distinct=True),
                answered=Count(
                    "responses",
                    filter=Q(responses__submitted_at__isnull=False),
                    distinct=True,
                ),
            )
        },
    )


@require_access()
@require_http_methods(["GET", "POST"])
def period_create(request):
    configuration, _created = PlanningSettings.objects.get_or_create(pk=1)
    defaults = configuration.snapshot()
    form = PeriodForm(
        request.POST or None,
        initial=defaults,
        defaults=defaults,
        reminder_weeks=configuration.next_period_weeks,
    )
    context = {"form": form}
    status = 200
    if request.method == "POST":
        # A rejected field must not throw the whole wizard back to its first step, so the
        # calendar and the following steps are rebuilt from whatever did survive validation.
        complete = form.is_valid()
        values = form.cleaned_data
        rules = form.rules()
        try:
            if not {"start_date", "end_date", "weekdays"} <= values.keys():
                raise ValidationError(_("Preview and select the dates before creating events."))
            days = calendar_days(
                values["start_date"],
                values["end_date"],
                values["weekdays"],
                rules["country"],
                rules["region"],
                values.get("exclude_holidays", False),
            )
            if request.POST.get("selection_ready") and request.POST.get("action") != "calendar":
                selected = form.selected_dates(request.POST.getlist("dates"))
            else:
                selected = [day["date"] for day in days if day["selected"]]
            for day in days:
                day["selected"] = day["date"] in selected
                day["column"] = day["date"].weekday() + 1
            context.update(days=days, selected_dates=selected)
            if complete and request.POST.get("action") == "create":
                if not request.POST.get("selection_ready"):
                    raise ValidationError(_("Preview and select the dates before creating events."))
                period = create_period(
                    request.user,
                    template_id=values["template"].pk,
                    start_date=values["start_date"],
                    end_date=values["end_date"],
                    dates=selected,
                    rules=rules,
                    creation_key=values["creation_key"],
                    next_reminder_on=form.reminder_day(),
                )
                messages.success(
                    request,
                    _("{count} services have been created.").format(count=len(selected)),
                )
                return redirect(period)
            preview = preview_template(values["template"], selected, settings.TIME_ZONE)
            context.update(preview=preview, shifts=preview[0]["shifts"] if preview else [])
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


def response_rows(period):
    """Coordinator overview of the frozen cohort and what it answered."""
    responses = list(
        period.responses.select_related("user").prefetch_related(
            "availabilities", "offered_shifts", "observer_events"
        )
    )
    shifts = period_shifts(period)
    cohort = eligibility([response.user for response in responses], shifts)
    for response in responses:
        offered = list(response.offered_shifts.all())
        current = {planned.pk for planned in cohort[response.user_id][1]}
        response.eligibility_warnings = [
            planned.snapshot["label"] for planned in offered if planned.pk not in current
        ]
        # A plain list keeps the scale order and avoids Counter's zero default in templates.
        counts = Counter(a.rating for a in response.availabilities.all())
        response.ratings = [
            (value, counts[value]) for value, _label in Availability.Rating.choices if counts[value]
        ]
        response.offered_count = len(offered)
        response.observer_count = len(response.observer_events.all())
    return sorted(
        responses, key=lambda response: (response.submitted_at is None, str(response.user))
    )


def render_period(request, period, form=None, status=200, closing=None):
    events = [
        {
            "date": link.date,
            "link": link,
            "missing": link.event is None,
            "event": link.event
            if link.event and request.user.has_perm("core.view_event", link.event)
            else None,
        }
        for link in period.events.all()
    ]
    responses = response_rows(period)
    capacity = None
    if form is None:
        if period.state == PlanningPeriod.State.PREPARATION:
            capacity = capacity_preview(period, period.suggestion.get("maximum"))
        form = SurveyOpeningForm(
            hint=_("Calculated for the current members: {sentence}").format(
                sentence=recommendation(capacity)
            )
            if capacity and recommendation(capacity)
            else "",
            initial={
                "expected_version": period.version,
                "deadline": period.deadline
                or timezone.now() + timedelta(days=period.rules["response_days"]),
                "reminder_days": ", ".join(map(str, period.rules["reminder_days"])),
                "maximum": period.suggestion.get("maximum")
                or (capacity["maximum"] if capacity else None),
            },
        )
    return render(
        request,
        "ephios_shift_coordination/period_detail.html",
        {
            "period": period,
            "events": events,
            "form": form,
            "closing": closing or SurveyClosingForm(initial={"expected_version": period.version}),
            "capacity": capacity,
            "recommendation": recommendation(period.suggestion),
            "responses": responses,
            "answered": sum(1 for response in responses if response.submitted_at),
        },
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
            open_now = request.POST.get("action") != "save_settings"
            open_survey(request.user, pk, **form.cleaned_data, open_now=open_now)
            messages.success(
                request,
                _("The survey is open and the invitations are on their way.")
                if open_now
                else _("The survey settings have been saved."),
            )
            return redirect(period)
        except (Conflict, ValidationError) as exc:
            form.add_error(
                None, str(exc) if isinstance(exc, Conflict) else ValidationError(exc.messages)
            )
            status = 409 if isinstance(exc, Conflict) else 200
    return render_period(request, period, form, status)


@require_access()
@require_http_methods(["POST"])
def survey_close(request, pk):
    period = get_object_or_404(PlanningPeriod, pk=pk)
    form = SurveyClosingForm(request.POST)
    status = 200
    if form.is_valid():
        try:
            close_survey(request.user, pk, expected_version=form.cleaned_data["expected_version"])
            messages.success(request, _("The survey is closed. You can finish the plan now."))
            return redirect("ephios_shift_coordination:plan", pk=pk)
        except (Conflict, ValidationError) as exc:
            form.add_error(
                None, str(exc) if isinstance(exc, Conflict) else ValidationError(exc.messages)
            )
            status = 409 if isinstance(exc, Conflict) else 200
    return render_period(request, period, status=status, closing=form)


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
                expected_version=form.cleaned_data["expected_version"],
                maximum=form.cleaned_data["maximum"],
                notes=form.cleaned_data["notes"],
                ratings=form.ratings(),
                observer_events=form.observer_events(),
            )
            messages.success(request, _("Thank you, your answer has been saved."))
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
        {
            "response": response,
            "form": form,
            "writable": writable,
            "recommendation": recommendation(response.period.suggestion),
            "services": own_services(request.user, response.period),
        },
        status=status,
    )


def own_services(user, period):
    """Published shifts of this member, with the replacement page to find cover."""
    if period.state != PlanningPeriod.State.PUBLISHED:
        return []
    rows = []
    for link in period.events.select_related("event").order_by("date"):
        if link.event is None or not user.has_perm("core.view_event", link.event):
            continue
        for planned in service_shifts(link):
            state = options(user, planned)
            if state["mine"]:
                rows.append({"planned": planned, "state": state, "link": link})
    return rows


def publication_context(user, pk):
    from .publication import load_publication, review_publication

    period = get_object_or_404(PlanningPeriod, pk=pk)
    if period.state == PlanningPeriod.State.PUBLISHED:
        context = load_publication(user, pk)
        context.update(
            version=period.publication_snapshot["draft_version"],
            fingerprint=period.publication_snapshot["fingerprint"],
            underfilled=period.publication_snapshot["underfilled"],
        )
        return context
    return review_publication(user, pk)


@require_access()
@require_http_methods(["GET"])
def publication(request, pk):
    from .forms import PublicationForm

    try:
        context = publication_context(request.user, pk)
        if context["period"].state != PlanningPeriod.State.PUBLISHED:
            context["form"] = PublicationForm(review=context)
        return render(request, "ephios_shift_coordination/publication.html", context)
    except (Conflict, ValidationError) as exc:
        return render(
            request,
            "ephios_shift_coordination/publication.html",
            {
                "period": get_object_or_404(PlanningPeriod, pk=pk),
                "error": str(exc) if isinstance(exc, Conflict) else " ".join(exc.messages),
            },
            status=409,
        )


@require_access()
@require_http_methods(["POST"])
def publish(request, pk):
    from .forms import PublicationForm
    from .publication import publish_plan

    context = {"period": get_object_or_404(PlanningPeriod, pk=pk)}
    status = 400
    try:
        context = publication_context(request.user, pk)
        form = PublicationForm(request.POST, review=context)
        context["form"] = form
        if set(request.POST) - {*form.fields, "csrfmiddlewaretoken"}:
            raise ValidationError(
                _("Publication accepts only the saved draft and its confirmations.")
            )
        if form.is_valid():
            publish_plan(request.user, pk, **form.cleaned_data)
            messages.success(request, _("The plan is published and everybody staffed is informed."))
            return redirect("ephios_shift_coordination:publication", pk=pk)
    except (Conflict, ValidationError) as exc:
        context["error"] = str(exc) if isinstance(exc, Conflict) else " ".join(exc.messages)
        status = 409 if isinstance(exc, Conflict) else 400
    return render(request, "ephios_shift_coordination/publication.html", context, status=status)


@require_access("member")
@require_http_methods(["GET"])
def replacement(request, pk):
    planned_event = service(pk)
    try:
        coordinator = replacement_access(request.user, planned_event)
    except Conflict as exc:
        return render(
            request,
            "ephios_shift_coordination/replacement.html",
            {"service": planned_event, "error": str(exc)},
            status=409,
        )
    shifts = [
        {**row, "options": options(request.user, row["planned"])}
        for row in replacements(planned_event, coordinator)
    ]
    return render(
        request,
        "ephios_shift_coordination/replacement.html",
        {
            "service": planned_event,
            "period": planned_event.period,
            "shifts": shifts,
            "coordinator": coordinator,
        },
    )


@require_access("member")
@require_http_methods(["POST"])
def staffing_action(request, pk):
    planned = get_object_or_404(PlannedShift.objects.select_related("event"), pk=pk)
    form = StaffingForm(request.POST)
    if form.is_valid():
        action = form.cleaned_data["action"]
        try:
            if action == "leave":
                remove_person(request.user, pk, user_id=form.cleaned_data["user_id"])
                messages.success(request, _("The shift has been updated. Thanks for telling us."))
            else:
                add_person(
                    request.user,
                    pk,
                    user_id=form.cleaned_data["user_id"],
                    observer=action == "observe",
                )
                messages.success(request, _("The shift has been updated. Thanks for helping out."))
        except (Conflict, ValidationError) as exc:
            messages.error(
                request, str(exc) if isinstance(exc, Conflict) else " ".join(exc.messages)
            )
    else:
        messages.error(request, _("This request was incomplete. Please try again."))
    if request.POST.get("next") == "event" and planned.event.event_id:
        return redirect(planned.event.event.get_absolute_url())
    return redirect("ephios_shift_coordination:replacement", pk=planned.event_id)


def assembly_rows(user, *, search="", event_type=None):
    """What the list shows per assembly: when it is, the own answer, and what to do next."""
    rows = [
        {
            "assembly": assembly,
            "shift": shift_of(assembly),
            "answer": answer_state(assembly, user),
            "options": answer_options(assembly, user),
            "minutes": list(assembly.minutes.all()),
        }
        for assembly in listed(user, search=search, event_type=event_type)
    ]
    rows = [row for row in rows if row["shift"]]
    now = timezone.now()
    return (
        sorted(
            (row for row in rows if row["shift"].end_time > now),
            key=lambda row: row["shift"].start_time,
        ),
        sorted(
            (row for row in rows if row["shift"].end_time <= now),
            key=lambda row: row["shift"].start_time,
            reverse=True,
        ),
    )


@require_access("member")
@require_http_methods(["GET"])
def assembly_list(request):
    search = request.GET.get("q", "").strip()
    kinds = listed_types(request.user)
    chosen = kinds.filter(pk=request.GET.get("type") or 0).first()
    upcoming, past = assembly_rows(request.user, search=search, event_type=chosen)
    return render(
        request,
        "ephios_shift_coordination/assembly_list.html",
        {
            "upcoming": upcoming,
            "past": past,
            "types": callable_types(request.user),
            "kinds": kinds,
            "chosen": chosen,
            "search": search,
        },
    )


@require_access("member")
@require_http_methods(["POST"])
def assembly_answer(request, pk):
    """Answer straight from the list; the signed link does the same for people in mail."""
    assembly = get_object_or_404(Assembly.objects.select_related("event"), pk=pk)
    if not request.user.has_perm("core.view_event", assembly.event):
        raise PermissionDenied
    try:
        answer(assembly, request.user, attending=request.POST.get("action") == "yes")
        messages.success(request, _("Thank you, your answer has been saved."))
    except (Conflict, ValidationError) as exc:
        messages.error(request, str(exc) if isinstance(exc, Conflict) else " ".join(exc.messages))
    return redirect(request.POST.get("next") or "ephios_shift_coordination:assembly_list")


@require_access("member")
@require_http_methods(["GET", "POST"])
def assembly_create(request):
    types = callable_types(request.user)
    if not types:
        raise PermissionDenied
    recipients = [
        {
            "type": event_type,
            "defaults": assembly_defaults(event_type),
            "people": would_invite(request.user, event_type),
        }
        for event_type in types
    ]
    form = AssemblyForm(
        request.POST or None,
        types=types,
        defaults={
            name: recipients[0]["defaults"][name] for name in ("title", "location", "description")
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            assembly = plan_assembly(request.user, **form.assembly_arguments())
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(
                request,
                _("The assembly has been called. The invitation has been sent.")
                if assembly.invited_at
                else _("The assembly has been called. Nobody has been notified yet."),
            )
            return redirect(assembly.get_absolute_url())
    return render(
        request,
        "ephios_shift_coordination/assembly_form.html",
        {"form": form, "recipients": recipients},
    )


@require_access("member")
@require_http_methods(["POST"])
def assembly_invite(request, pk):
    assembly = get_object_or_404(Assembly.objects.select_related("event"), pk=pk)
    recipients = invite(request.user, assembly)
    messages.success(
        request,
        ngettext(
            "The invitation has been sent to %(count)d person.",
            "The invitation has been sent to %(count)d people.",
            len(recipients),
        )
        % {"count": len(recipients)},
    )
    return redirect(assembly.get_absolute_url())


@login_not_required
@require_http_methods(["GET", "POST"])
def assembly_respond(request, token):
    """Answer straight from the invitation mail: the signed link stands in for the login."""
    if not enabled():
        raise Http404
    try:
        assembly, user = resolve_answer(token)
    except Conflict as exc:
        return render(
            request,
            "ephios_shift_coordination/assembly_respond.html",
            {"error": str(exc)},
            status=409,
        )
    error = None
    if request.method == "POST":
        try:
            answer(assembly, user, attending=request.POST.get("action") == "yes")
        except (Conflict, ValidationError) as exc:
            error = str(exc) if isinstance(exc, Conflict) else " ".join(exc.messages)
    return render(
        request,
        "ephios_shift_coordination/assembly_respond.html",
        {
            "assembly": assembly,
            "person": user,
            "shift": shift_of(assembly),
            "answer": answer_state(assembly, user),
            "answered": request.method == "POST" and not error,
            "error": error,
        },
        status=409 if error else 200,
    )


@require_access("member")
@require_http_methods(["POST"])
def minutes_upload(request, pk):
    assembly = get_object_or_404(Assembly.objects.select_related("event"), pk=pk)
    form = MinutesForm(request.POST, request.FILES)
    if form.is_valid():
        file_minutes(request.user, assembly, form.cleaned_data["file"])
        messages.success(request, _("The assembly minutes have been filed."))
    else:
        messages.error(request, " ".join(" ".join(errors) for errors in form.errors.values()))
    return redirect(assembly.get_absolute_url())


@require_access("member")
@require_http_methods(["POST"])
def minutes_delete(request, pk):
    minutes = get_object_or_404(AssemblyMinutes.objects.select_related("assembly__event"), pk=pk)
    assembly = minutes.assembly
    remove(request.user, minutes)
    messages.success(request, _("The assembly minutes have been deleted."))
    return redirect(assembly.get_absolute_url())


@require_access("member")
@require_http_methods(["GET"])
def minutes_file(request, pk):
    """Serve the PDF for reading in the browser.

    ephios' own accelerated media response always says attachment, so this view builds its
    own; the media directory itself stays unreachable from the web either way.
    """
    minutes = get_object_or_404(AssemblyMinutes.objects.select_related("assembly__event"), pk=pk)
    if not request.user.has_perm("core.view_event", minutes.assembly.event):
        raise PermissionDenied
    response = FileResponse(minutes.file.open("rb"), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{display_name(minutes)}"'
    return response
