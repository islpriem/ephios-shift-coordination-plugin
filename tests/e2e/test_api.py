"""The public duty information: off by default, and readable without a session once on."""

import json
import os
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from tests.e2e.test_surveys import in_test_app

pytestmark = pytest.mark.e2e


def ask(path):
    """A plain HTTP request without any cookie: exactly what a wall display sends."""
    base = f"http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8099')}"
    try:
        with urlopen(f"{base}/shift-coordination/api/{path}/", timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, None


def test_the_duty_information_is_off_until_it_is_switched_on():
    for path in ("now", "next", "week"):
        assert ask(path)[0] == 404

    in_test_app("""
from ephios.core.models import EventType
from ephios_shift_coordination.models import PlanningSettings
settings = PlanningSettings.objects.get()
settings.api_enabled = True
settings.save()
settings.api_event_types.set(EventType.objects.filter(title='Dienst'))
print('on')
""")
    try:
        for path in ("now", "next", "week"):
            status, answer = ask(path)
            assert status == 200, path
            assert "demo" not in json.dumps(answer).lower(), path
        assert isinstance(ask("now")[1]["duty"], bool)
        assert len(ask("week")[1]["days"]) == 7
        # The demo publishes a plan that starts within the week, so a duty is coming.
        assert ask("next")[1]["start"] is not None
    finally:
        in_test_app("""
from ephios_shift_coordination.models import PlanningSettings
PlanningSettings.objects.filter(pk=1).update(api_enabled=False)
print('off')
""")
