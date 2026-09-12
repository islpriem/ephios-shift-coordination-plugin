import pytest
from django.apps import apps
from dynamic_preferences.registries import global_preferences_registry
from ephios.core.plugins import get_all_plugins, get_enabled_plugins


@pytest.mark.django_db
def test_plugin_can_be_enabled_and_disabled():
    plugin = next(p for p in get_all_plugins() if p.module == "ephios_shift_coordination")
    assert plugin.app == apps.get_app_config("ephios_shift_coordination")
    preferences = global_preferences_registry.manager()
    original = preferences["general__enabled_plugins"]
    preferences["general__enabled_plugins"] = [*original, plugin.module]
    assert plugin in get_enabled_plugins()
    preferences["general__enabled_plugins"] = original
    assert plugin not in get_enabled_plugins()
