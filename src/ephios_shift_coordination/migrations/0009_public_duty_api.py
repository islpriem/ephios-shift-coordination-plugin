from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_alter_userprofile_disabled_notifications"),
        ("ephios_shift_coordination", "0008_assemblyminutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="planningsettings",
            name="api_enabled",
            field=models.BooleanField(default=False, verbose_name="Public duty information"),
        ),
        migrations.AddField(
            model_name="planningsettings",
            name="api_event_types",
            field=models.ManyToManyField(
                blank=True,
                to="core.eventtype",
                verbose_name="Event types in the public information",
            ),
        ),
    ]
