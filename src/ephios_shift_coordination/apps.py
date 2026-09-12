from django.utils.translation import gettext_lazy as _
from ephios.core.plugins import PluginConfig


class PluginApp(PluginConfig):
    name = "ephios_shift_coordination"
    verbose_name = _("Shift coordination")

    class EphiosPluginMeta:
        name = _("Shift coordination")
        author = "Felix Triebel <felix@triebel.me>"
        description = _("Availability surveys and service planning for ephios")

    def ready(self):
        from . import signals  # noqa: F401
