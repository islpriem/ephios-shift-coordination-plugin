"""Reminders before a service or an assembly: one message per person and appointment.

A rule is instance wide and says how many days before the appointment a reminder goes out
and at what local time. The ledger of sent reminders makes repeated periodic runs harmless;
a reminder whose moment passed during downtime is recorded without being sent, because a
reminder that arrives after the appointment only confuses people.
"""

from datetime import datetime, timedelta

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone
from ephios.core.models import AbstractParticipation, EventType, LocalParticipation, Shift
from ephios.core.models.users import Notification

from .access import enabled
from .dates import local_datetime
from .ephios_integration import is_assembly, is_service
from .models import Assembly, ObserverParticipation, PlanningSettings, ReminderDispatch

CONFIRMED = AbstractParticipation.States.CONFIRMED
KINDS = ("service", "assembly")


def configuration():
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    return {
        kind: (
            getattr(settings, f"{kind}_reminder_days") or [],
            getattr(settings, f"{kind}_reminder_time"),
        )
        for kind in KINDS
    }


def marked_types(kind):
    """The event types a rule of this kind applies to; an instance has a handful of them."""
    test = is_service if kind == "service" else is_assembly
    return [event_type.pk for event_type in EventType.objects.all() if test(event_type)]


def moments(shift, offsets, clock_time):
    """Every moment the rules ask for, oldest first, in the shift's local calendar."""
    zone = django_settings.TIME_ZONE
    day = timezone.localtime(shift.start_time).date()
    return sorted(
        {local_datetime(day - timedelta(days=offset), clock_time, zone) for offset in offsets}
    )


def due(shift, offsets, clock_time, now):
    return [moment for moment in moments(shift, offsets, clock_time) if moment <= now]


def service_recipients(shift):
    """Everybody confirmed for the shift, including the people sitting in."""
    users = set(
        LocalParticipation.objects.filter(shift=shift, state=CONFIRMED).values_list(
            "user_id", flat=True
        )
    )
    return users | set(
        ObserverParticipation.objects.filter(
            planned_shift__shift=shift, participation__state=CONFIRMED
        ).values_list("user_id", flat=True)
    )


def assembly_recipients(shift):
    """Everybody invited, whatever they answered: the assembly concerns all of them."""
    from .assemblies import invited

    return set(invited(shift.event).values_list("pk", flat=True))


RECIPIENTS = {"service": service_recipients, "assembly": assembly_recipients}


def payload(kind, shift):
    """The assembly handlers work from the assembly, the service ones from the shift."""
    if kind == "service":
        return {"shift_id": shift.pk}
    assembly = Assembly.objects.filter(event_id=shift.event_id).first()
    return {"assembly_id": assembly.pk} if assembly else None


def dispatch(shift, user_id, kind, moment, data, *, skipped):
    record, created = ReminderDispatch.objects.get_or_create(
        shift=shift,
        user_id=user_id,
        key=f"{kind}:{moment.isoformat()}",
        defaults={"skipped": skipped},
    )
    if created and not skipped:
        record.notification = Notification.objects.create(
            user_id=user_id, slug=f"shift_coordination_{kind}_reminder", data=data
        )
        record.save(update_fields=["notification"])


def upcoming_shifts(kind, offsets, now):
    return (
        Shift.objects.filter(
            event__active=True,
            event__type_id__in=marked_types(kind),
            start_time__gt=now,
            start_time__lte=now + timedelta(days=max(offsets) + 1),
        )
        .select_related("event")
        .order_by("start_time")
    )


def process_reminders():
    """Called from the periodic signal: send what is due, record what came too late."""
    if not enabled():
        return
    now = timezone.now()
    for kind, (offsets, clock_time) in configuration().items():
        if not offsets:
            continue
        for shift in upcoming_shifts(kind, offsets, now):
            instants = due(shift, offsets, clock_time, now)
            data = payload(kind, shift) if instants else None
            if not instants or data is None:
                continue
            with transaction.atomic():
                for user_id in RECIPIENTS[kind](shift):
                    for moment in instants:
                        dispatch(
                            shift,
                            user_id,
                            kind,
                            moment,
                            data,
                            skipped=moment != instants[-1],
                        )


def overview(shift):
    """What the people responsible see: reminders already sent and the next one to come."""
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    kind = "assembly" if is_assembly(shift.event.type) else "service"
    offsets = getattr(settings, f"{kind}_reminder_days") or []
    clock_time = getattr(settings, f"{kind}_reminder_time")
    now = timezone.now()
    sent = sorted(
        {
            record.key.split(":", 1)[1]
            for record in ReminderDispatch.objects.filter(shift=shift, skipped=False)
        }
    )
    later = [moment for moment in moments(shift, offsets, clock_time) if moment > now]
    return {
        "sent": [datetime.fromisoformat(moment) for moment in sent],
        "next": later[0] if later else None,
    }
