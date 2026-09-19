import tomllib
from importlib.metadata import entry_points, version
from pathlib import Path


def test_plugin_is_discovered_from_installed_distribution():
    plugins = entry_points(group="ephios.plugins")
    plugin = next((entry for entry in plugins if entry.name == "ephios_shift_coordination"), None)
    assert plugin is not None, "The installed distribution must register its ephios plugin."
    assert plugin.value == "ephios_shift_coordination.apps.PluginApp"
    # What is installed has to be what this checkout declares, whatever the number is.
    declared = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    assert version("ephios-shift-coordination-plugin") == declared
