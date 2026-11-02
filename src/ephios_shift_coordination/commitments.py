"""Read native observer participations using their effective participation times."""

from django.db.models import Q
from ephios.core.models.events import PlaceholderParticipation

from .scheduling import Commitment


def observer_commitments(user_ids, event_type_id, lower, upper, start, end, zone):
    return tuple(
        Commitment(
            row["pk"],
            row["planning_observer__user_id"],
            row["shift__event__type_id"],
            row["start_time"],
            row["end_time"],
            row["start_time"].astimezone(zone).date(),
            event_id=row["shift__event_id"],
            observer=True,
        )
        for row in PlaceholderParticipation.objects.filter(
            planning_observer__user_id__in=user_ids,
            state=PlaceholderParticipation.States.CONFIRMED,
            shift__event__type_id=event_type_id,
        )
        .filter(
            Q(start_time__gte=lower, start_time__lt=upper)
            | Q(start_time__lt=end, end_time__gt=start)
        )
        .order_by("pk")
        .values(
            "pk",
            "planning_observer__user_id",
            "shift__event_id",
            "shift__event__type_id",
            "start_time",
            "end_time",
        )
    )
