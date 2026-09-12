from datetime import date, time

import pytest
from django.core.exceptions import ValidationError

from ephios_shift_coordination.dates import calendar_days, local_datetime, next_month


@pytest.mark.parametrize(
    "today, start, end",
    [
        (date(2027, 12, 8), date(2028, 1, 1), date(2028, 1, 31)),
        (date(2028, 1, 31), date(2028, 2, 1), date(2028, 2, 29)),
        (date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 28)),
    ],
)
def test_next_complete_month(today, start, end):
    assert next_month(today) == (start, end)


def test_calendar_marks_regional_holidays_and_weekday_defaults():
    days = calendar_days(date(2026, 3, 8), date(2026, 3, 10), [1, 6], "DE", "BE", True)
    assert days[0]["holiday"]
    assert not days[0]["selected"]
    assert not days[1]["selected"]
    assert days[2]["selected"]
    assert calendar_days(date(2026, 3, 8), date(2026, 3, 8), [6], "DE", "BE", False)[0]["selected"]


@pytest.mark.parametrize("region, excluded", [("", True), ("invalid", True), ("invalid", False)])
def test_calendar_rejects_invalid_holiday_setup(region, excluded):
    with pytest.raises(ValidationError):
        calendar_days(date(2026, 3, 8), date(2026, 3, 9), [6], "DE", region, excluded)


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_nonexistent_and_ambiguous_local_times_are_rejected(day):
    with pytest.raises(ValidationError):
        local_datetime(day, time(2, 30), "Europe/Berlin")


def test_local_datetime_preserves_valid_time_and_timezone():
    value = local_datetime(date(2026, 3, 29), time(3, 30), "Europe/Berlin")
    assert value.isoformat() == "2026-03-29T03:30:00+02:00"


def test_calendar_rejects_reversed_range():
    with pytest.raises(ValidationError):
        calendar_days(date(2026, 4, 1), date(2026, 3, 1), [1], "DE", "", False)
