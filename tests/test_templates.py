from datetime import time

import pytest
from django.core.exceptions import ValidationError

from ephios_shift_coordination.models import PlanningSettings, ShiftTemplate


def test_settings_require_a_region_before_excluding_holidays():
    config = PlanningSettings(exclude_holidays=True)
    with pytest.raises(ValidationError, match="region"):
        config.clean()
    config.region = "BE"
    config.clean()


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum": 0},
        {"maximum": 1},
        {"end_time": time(8)},
        {"meeting_time": time(10)},
        {"end_day_offset": 2},
    ],
)
def test_shift_template_rejects_invalid_capacity_or_times(changes):
    shift = ShiftTemplate(
        label="Phone",
        start_time=time(9),
        end_time=time(13),
        meeting_time=time(8, 45),
        minimum=2,
        maximum=3,
    )
    for key, value in changes.items():
        setattr(shift, key, value)
    with pytest.raises(ValidationError):
        shift.clean()


def test_shift_template_allows_overnight_duties_and_unlimited_maximum():
    shift = ShiftTemplate(
        label="Night",
        start_time=time(22),
        end_time=time(6),
        meeting_time=time(21, 45),
        end_day_offset=1,
        minimum=2,
        maximum=None,
    )
    shift.clean()


@pytest.mark.parametrize(
    "changes",
    [
        {"weekdays": [7]},
        {"weekdays": [1, 1]},
        {"weekdays": "Tuesday"},
        {"reminder_days": [0]},
        {"reminder_days": [3, 3]},
        {"reminder_days": "three"},
    ],
)
def test_invalid_planning_rules_are_rejected(changes):
    instance = PlanningSettings(**changes)
    with pytest.raises(ValidationError):
        instance.clean()


def test_every_plugin_string_has_a_reviewed_german_translation():
    import re
    from pathlib import Path

    catalog = Path("src/ephios_shift_coordination/locale/de/LC_MESSAGES/django.po").read_text()
    entries = re.findall(r'msgid (".*?")\nmsgstr (".*?")\n', catalog, re.S)
    assert [msgid for msgid, msgstr in entries if msgstr == '""' and msgid != '""'] == []
    # Fuzzy entries are guesses that Django would not use, obsolete ones are dead weight.
    assert "#, fuzzy" not in catalog and "#~" not in catalog
