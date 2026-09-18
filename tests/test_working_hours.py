"""Keeping the working hours out of sight for everybody but the administration."""

import pytest
from django.urls import reverse

from ephios_shift_coordination.models import PlanningSettings


def hide(value=True):
    settings, _created = PlanningSettings.objects.get_or_create(pk=1)
    settings.hide_working_hours = value
    settings.save()


PAGES = ["core:workinghours_own", "core:workinghours_list"]


@pytest.mark.django_db
def test_the_working_hours_stay_open_until_somebody_hides_them(planning_data, client):
    client.force_login(planning_data.member)
    assert client.get(reverse("core:workinghours_own")).status_code == 200
    assert "workinghours" in client.get(reverse("core:home")).content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("page", PAGES)
def test_hidden_working_hours_are_refused_for_everybody_but_the_administration(
    planning_data, client, page
):
    hide()
    client.force_login(planning_data.member)
    assert client.get(reverse(page)).status_code == 403
    client.force_login(planning_data.coordinator)
    assert client.get(reverse(page)).status_code == 403
    # An administrator keeps the pages, otherwise nobody could grant working hours any more.
    client.force_login(planning_data.admin)
    assert client.get(reverse(page)).status_code == 200


@pytest.mark.django_db
def test_hiding_also_removes_the_links_ephios_writes_itself(planning_data, client):
    hide()
    client.force_login(planning_data.member)
    page = client.get(reverse("core:home")).content.decode()
    # The rule needs the request nonce, or ephios' own content security policy drops it.
    assert 'a[href^="/workinghours/"]' in page
    assert "<style nonce=" in page
    client.force_login(planning_data.admin)
    assert 'a[href^="/workinghours/"]' not in client.get(reverse("core:home")).content.decode()


@pytest.mark.django_db
def test_a_switched_off_plugin_never_hides_anything(planning_data, client):
    from dynamic_preferences.registries import global_preferences_registry

    hide()
    preferences = global_preferences_registry.manager()
    preferences["general__enabled_plugins"] = [
        name
        for name in preferences["general__enabled_plugins"]
        if name != "ephios_shift_coordination"
    ]
    client.force_login(planning_data.member)
    assert client.get(reverse("core:workinghours_own")).status_code == 200
