import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


def next_month(today):
    start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


def holiday_calendar(country, region, excluded):
    regions = holidays.list_supported_countries().get(country)
    if regions is None or (region and region not in regions) or (excluded and not region):
        raise ValidationError(_("Choose a valid holiday country and region."))
    return holidays.country_holidays(country, subdiv=region or None, language="de")


def calendar_days(start, end, weekdays, country, region, excluded):
    if start > end:
        raise ValidationError(_("The end date must not precede the start date."))
    holiday_dates = holiday_calendar(country, region, excluded)
    return [
        {
            "date": day,
            "holiday": holiday_dates.get(day, ""),
            "selected": day.weekday() in weekdays and not (excluded and day in holiday_dates),
        }
        for offset in range((end - start).days + 1)
        for day in [start + timedelta(days=offset)]
    ]


def local_datetime(day, clock_time, timezone_name):
    naive = datetime.combine(day, clock_time)
    zone = ZoneInfo(timezone_name)
    candidates = {
        candidate.astimezone(UTC): candidate
        for fold in (0, 1)
        for candidate in [naive.replace(tzinfo=zone, fold=fold)]
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
    }
    if len(candidates) != 1:
        raise ValidationError(
            _("The local time %(time)s is ambiguous or does not exist in %(zone)s."),
            params={"time": str(naive), "zone": timezone_name},
        )
    return next(iter(candidates.values()))
