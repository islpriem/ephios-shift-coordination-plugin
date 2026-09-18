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


def plugin_assets(suffix):
    from pathlib import Path

    root = Path("src/ephios_shift_coordination")
    return sorted(root.glob(f"templates/ephios_shift_coordination/*{suffix}")) + sorted(
        root.glob(f"static/ephios_shift_coordination/*{suffix}")
    )


def test_no_interface_element_uses_the_muted_secondary_button():
    """The secondary button reads as disabled in the ephios colour scheme."""
    offenders = [
        path.name
        for suffix in (".html", ".js")
        for path in plugin_assets(suffix)
        if "btn-outline-secondary" in path.read_text() or "btn-secondary" in path.read_text()
    ]
    assert offenders == []


def test_every_availability_rating_has_its_own_column_colour():
    import re
    from pathlib import Path

    style = Path(
        "src/ephios_shift_coordination/static/ephios_shift_coordination/planning.css"
    ).read_text()
    tints = {}
    for value in ("unavailable", "if_needed", "available", "preferred"):
        match = re.search(rf"\.sc-r-{value} \{{(.*?)\}}", style, re.S)
        assert match, value
        tints[value] = re.search(r"--sc-tint: ([^;]+);", match.group(1)).group(1)
    assert len(set(tints.values())) == len(tints), tints
    # The rating columns are separated by their own edge colour, not only by the first one.
    assert "border-inline: 1px solid var(--sc-edge)" in style
