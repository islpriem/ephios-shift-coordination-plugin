from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from ephios.core.models import Event, UserProfile

from ephios_shift_coordination.models import PlanningPeriod
from ephios_shift_coordination.services import create_period
from tests.test_periods import request_data

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_parallel_creation_commits_exactly_one_series(planning_data):
    assert connection.vendor == "postgresql"
    barrier = Barrier(2)
    arguments = request_data(planning_data)
    user_id = planning_data.coordinator.pk

    def submit():
        close_old_connections()
        try:
            user = UserProfile.objects.get(pk=user_id)
            barrier.wait(timeout=10)
            return create_period(user, **arguments).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: submit(), range(2)))
    assert first == second
    assert PlanningPeriod.objects.count() == 1
    assert Event.objects.count() == 2
