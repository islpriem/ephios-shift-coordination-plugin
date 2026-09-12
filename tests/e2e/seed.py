import os
from datetime import date

from dynamic_preferences.registries import global_preferences_registry
from ephios.core.models import UserProfile

if os.environ.get("EPHIOS_TESTING") != "1":
    raise RuntimeError("Synthetic users may only be created in the isolated test stack.")

for language in ("en", "de"):
    user, _ = UserProfile.objects.update_or_create(
        email=f"admin-{language}@example.invalid",
        defaults={
            "display_name": "Integration Administrator",
            "date_of_birth": date(1990, 1, 1),
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
            "preferred_language": language,
        },
    )
    user.set_password("isolated-browser-test-password")
    user.save()
preferences = global_preferences_registry.manager()
preferences["general__enabled_plugins"] = sorted(
    set(
        [
            *preferences["general__enabled_plugins"],
            "ephios_shift_coordination",
        ]
    )
)
