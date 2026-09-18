"""Per event type options: what kind of event this is, and the assembly defaults.

ephios has no plugin hook for the event type form, but it builds that form from the
per instance preference registry. Registering here puts the options into the existing
editor instead of copying core markup into a second settings page. The registry is
filled on import, so the options appear as soon as the package is installed.
"""

from django.utils.translation import gettext_lazy as _
from dynamic_preferences.preferences import Section
from dynamic_preferences.types import BooleanPreference, StringPreference
from ephios.core.dynamic_preferences_registry import event_type_preference_registry

SECTION = Section("shift_coordination", verbose_name=_("Shift coordination"))


@event_type_preference_registry.register
class IsServicePreference(BooleanPreference):
    section = SECTION
    name = "is_service"
    verbose_name = _("Events of this type are services")
    help_text = _("Everybody staffed for a service is reminded before it starts.")
    default = False


@event_type_preference_registry.register
class IsAssemblyPreference(BooleanPreference):
    section = SECTION
    name = "is_assembly"
    verbose_name = _("Events of this type are assemblies")
    help_text = _(
        "Assemblies can be called with a short form, invite everybody who may see them, "
        "and take an agenda and assembly minutes."
    )
    default = False


@event_type_preference_registry.register
class AssemblyTitlePreference(StringPreference):
    section = SECTION
    name = "assembly_title"
    verbose_name = _("Default title for assemblies of this type")
    default = ""
    required = False


@event_type_preference_registry.register
class AssemblyLocationPreference(StringPreference):
    section = SECTION
    name = "assembly_location"
    verbose_name = _("Default location for assemblies of this type")
    default = ""
    required = False
