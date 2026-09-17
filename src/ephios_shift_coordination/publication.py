"""Publish a saved draft through native participations and retain its historical record."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from ephios.core.models import Event, LocalParticipation, Shift, UserProfile
from ephios.core.models.events import PlaceholderParticipation
from ephios.core.models.users import Notification
from ephios.core.services.notifications.backends import send_all_notifications

from .access import can_plan, enabled
from .drafts import RULE_LABELS, checked, snapshot
from .ephios_integration import observer_name
from .models import NotificationDispatch, ObserverParticipation, PlannedShift, PlanningPeriod
from .scheduling import fingerprint as digest
from .services import Conflict


def check_access(user, period):
    user = UserProfile.objects.filter(pk=user.pk).first()
    if not user or not enabled() or not can_plan(user):
        raise PermissionDenied
    events = Event.all_objects.filter(planning_link__period=period)
    moved = Event.all_objects.filter(shifts__plannedshift__event__period=period)
    for event in events.union(moved):
        if not (
            user.has_perm("core.view_event", event) and user.has_perm("core.change_event", event)
        ):
            raise PermissionDenied


def saved_plan(current):
    period = current.period
    if period.effective_state != PlanningPeriod.State.PLANNING:
        raise Conflict(_("Close the survey before publishing this plan."))
    if not period.draft_fingerprint or period.draft_fingerprint != current.fingerprint:
        raise Conflict(_("Save a freshly checked shared draft before reviewing publication."))
    saved = list(
        period.draft_assignments.order_by("original_user_id", "planned_shift_id").values_list(
            "original_user_id", "planned_shift_id", "observer"
        )
    )
    assignments = [[uid, sid] for uid, sid, observer in saved if not observer]
    observers = [[uid, sid] for uid, sid, observer in saved if observer]
    result = checked(current, period.version, current.fingerprint, assignments, observers)
    overrides = list(period.rule_overrides.filter(draft_version=period.version).order_by("pk"))
    if {record.token for record in overrides} != {v.token for v in result.violations}:
        raise Conflict(_("Save a freshly checked shared draft before reviewing publication."))
    return assignments, observers, result, overrides


def person_names(ids):
    users = UserProfile.objects.in_bulk(ids)
    return {
        uid: str(users[uid]) if uid in users else _("Deleted person #{id}").format(id=uid)
        for uid in ids
    }


def override_rows(period, ids):
    rows = list(period.rule_overrides.filter(pk__in=ids).select_related("actor").order_by("pk"))
    names = person_names({uid for row in rows for uid in row.facts["person_ids"]})
    shifts = PlannedShift.objects.filter(event__period=period).in_bulk()
    zone = ZoneInfo(period.timezone)
    for row in rows:
        row.rule_label = RULE_LABELS[row.code]
        row.people_label = ", ".join(names[uid] for uid in row.facts["person_ids"])
        labels = []
        for sid in row.facts["shift_ids"]:
            shift = shifts[sid].snapshot
            start = date_format(
                timezone.localtime(datetime.fromisoformat(shift["start_time"]), zone),
                "DATETIME_FORMAT",
            )
            labels.append(f"{start} · {shift['label']}")
        row.shifts_label = "; ".join(labels)
    return rows


@transaction.atomic
def review_publication(user, period_id):
    current = snapshot(user, period_id)
    assignments, observers, result, overrides = saved_plan(current)
    names = {person["id"]: person["name"] for person in current.people}
    staffed = {sid for uid, sid in assignments + observers}
    return {
        "period": current.period,
        "version": current.period.version,
        "fingerprint": current.fingerprint,
        "filled": len(staffed - {sid for sid, _count, _minimum in result.underfilled}),
        "total": len(current.shifts),
        "rows": [
            {
                **shift,
                "start": datetime.fromisoformat(shift["start"]),
                "end": datetime.fromisoformat(shift["end"]),
                "people": [names[uid] for uid, sid in assignments if sid == shift["id"]],
                "observers": [names[uid] for uid, sid in observers if sid == shift["id"]],
                "staffed": sum(sid == shift["id"] for uid, sid in assignments + observers),
            }
            for shift in current.shifts
        ],
        "recipients": [names[uid] for uid in sorted({uid for uid, sid in assignments + observers})],
        "underfilled": [list(row) for row in result.underfilled],
        "overrides": override_rows(current.period, [record.pk for record in overrides]),
    }


@transaction.atomic
def publish_plan(
    user,
    period_id,
    *,
    expected_version,
    fingerprint,
    confirmed_tokens,
    confirm_underfilled,
    confirm_publish,
):
    period = get_object_or_404(PlanningPeriod.objects.select_for_update(), pk=period_id)
    check_access(user, period)
    if (
        type(expected_version) is not int
        or not isinstance(fingerprint, str)
        or not isinstance(confirmed_tokens, list)
        or any(not isinstance(token, str) for token in confirmed_tokens)
        or len(set(confirmed_tokens)) != len(confirmed_tokens)
        or type(confirm_underfilled) is not bool
        or confirm_publish is not True
    ):
        raise ValidationError(_("Explicitly confirm the saved plan and all publication warnings."))
    request_digest = digest(
        {
            "version": expected_version,
            "fingerprint": fingerprint,
            "tokens": sorted(confirmed_tokens),
            "underfilled": confirm_underfilled,
        }
    )
    if period.state == PlanningPeriod.State.PUBLISHED:
        if period.publication_snapshot.get("request_digest") != request_digest:
            raise Conflict(_("This period is already published. Its record cannot be replaced."))
        return period
    current = snapshot(user, period_id, lock=True)
    if expected_version != current.period.version or fingerprint != current.fingerprint:
        raise Conflict(_("The draft or its inputs have changed. Reload before continuing."))
    assignments, observers, result, overrides = saved_plan(current)
    if set(confirmed_tokens) != {v.token for v in result.violations} or (
        result.underfilled and not confirm_underfilled
    ):
        raise ValidationError(_("Explicitly confirm the saved plan and all publication warnings."))
    period = current.period
    links = list(period.events.order_by("date").prefetch_related("shifts__shift"))
    targets = {link.pk: link for event in links for link in event.shifts.all()}
    participations = {}
    for uid, sid in assignments:
        participation = LocalParticipation(
            user_id=uid, shift=targets[sid].shift, state=LocalParticipation.States.CONFIRMED
        )
        # Native save retains visibility and model logging without disposition's per-shift mails.
        participation.save()
        participations[uid, sid] = participation.pk
    names = person_names({uid for uid, sid in observers})
    for uid, sid in observers:
        # People sitting in join as native placeholders: staffed, but without working hours.
        placeholder = PlaceholderParticipation(
            shift=targets[sid].shift,
            display_name=observer_name(names[uid]),
            state=PlaceholderParticipation.States.CONFIRMED,
        )
        placeholder.save()
        ObserverParticipation.objects.create(
            participation=placeholder, planned_shift=targets[sid], user_id=uid
        )
        participations[uid, sid] = placeholder.pk
    period.published_at = timezone.now()
    period.published_by_id = user.pk
    period.publication_snapshot = {
        "schema_version": 1,
        "draft_version": period.version,
        "fingerprint": current.fingerprint,
        "request_digest": request_digest,
        "actor_id": user.pk,
        "override_ids": [record.pk for record in overrides],
        "underfilled": [list(row) for row in result.underfilled],
        "shifts": [
            {
                "id": link.pk,
                "native_id": link.shift_id,
                "event_id": link.event.event_id,
                "event_type_id": period.template_snapshot["event_type"],
                **link.snapshot,
                "members": [
                    {
                        "user_id": uid,
                        "participation_id": participations[uid, sid],
                        "observer": observer,
                    }
                    for entries, observer in ((assignments, False), (observers, True))
                    for uid, sid in entries
                    if sid == link.pk
                ],
            }
            for link in targets.values()
        ],
    }
    period.version += 1
    period.state = PlanningPeriod.State.PUBLISHED
    period.save(
        update_fields=["version", "state", "published_at", "published_by", "publication_snapshot"]
    )
    for uid in sorted({uid for uid, sid in assignments + observers}):
        dispatch = NotificationDispatch.objects.create(
            period=period, user_id=uid, kind="publication", key=f"version:{period.version}"
        )
        dispatch.notification = Notification.objects.create(
            user_id=uid,
            slug="shift_coordination_published",
            data={"period_id": period.pk, "dispatch_key": dispatch.key},
        )
        dispatch.save(update_fields=["notification"])
    transaction.on_commit(send_all_notifications, robust=True)
    return period


@transaction.atomic
def load_publication(user, period_id):
    period = get_object_or_404(PlanningPeriod.objects.select_related("published_by"), pk=period_id)
    check_access(user, period)
    if period.state != PlanningPeriod.State.PUBLISHED:
        raise Conflict(_("This period has not been published."))
    record = period.publication_snapshot
    names = person_names(
        {member["user_id"] for shift in record["shifts"] for member in shift["members"]}
    )
    native = (
        Shift.objects.filter(pk__in=[s["native_id"] for s in record["shifts"]])
        .select_related("event")
        .prefetch_related("participations")
        .in_bulk()
    )
    rows = []
    for shift in record["shifts"]:
        current = native.get(shift["native_id"])
        changed = current is None
        if current:
            expected = {
                (m["participation_id"], None if m.get("observer") else m["user_id"])
                for m in shift["members"]
            }
            existing = list(current.participations.all())
            changed = (
                not current.event.active
                or current.event_id != shift["event_id"]
                or current.event.type_id != shift["event_type_id"]
                or current.label != shift["label"]
                or current.signup_flow_slug != "manual"
                or current.structure_slug != "uniform"
                or current.structure_configuration != shift["structure_configuration"]
                or any(
                    getattr(current, field) != datetime.fromisoformat(shift[field])
                    for field in ("meeting_time", "start_time", "end_time")
                )
                or {(p.pk, getattr(p, "user_id", None)) for p in existing} != expected
                or any(
                    p.state != LocalParticipation.States.CONFIRMED
                    or p.start_time != current.start_time
                    or p.end_time != current.end_time
                    or p.structure_data
                    for p in existing
                )
            )
        rows.append(
            {
                "id": shift["id"],
                "label": shift["label"],
                "start": datetime.fromisoformat(shift["start_time"]),
                "end": datetime.fromisoformat(shift["end_time"]),
                "minimum": shift["structure_configuration"]["minimum_number_of_participants"],
                "people": [names[m["user_id"]] for m in shift["members"] if not m.get("observer")],
                "observers": [names[m["user_id"]] for m in shift["members"] if m.get("observer")],
                "changed": bool(changed),
                "url": current.get_absolute_url() if current else None,
            }
        )
    return {
        "period": period,
        "rows": rows,
        "changed": any(row["changed"] for row in rows),
        "filled": sum(
            1 for row in rows if len(row["people"]) + len(row["observers"]) >= row["minimum"]
        ),
        "total": len(rows),
        "overrides": override_rows(period, record["override_ids"]),
    }
