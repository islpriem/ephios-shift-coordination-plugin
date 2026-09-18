import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import ephios_shift_coordination.models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_alter_userprofile_disabled_notifications"),
        ("ephios_shift_coordination", "0006_assembly"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="planningsettings",
            name="assembly_reminder_days",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Assembly reminders in days before"
            ),
        ),
        migrations.AddField(
            model_name="planningsettings",
            name="assembly_reminder_time",
            field=models.TimeField(
                default=ephios_shift_coordination.models.nine_o_clock,
                verbose_name="Assembly reminder time",
            ),
        ),
        migrations.AddField(
            model_name="planningsettings",
            name="service_reminder_days",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Service reminders in days before"
            ),
        ),
        migrations.AddField(
            model_name="planningsettings",
            name="service_reminder_time",
            field=models.TimeField(
                default=ephios_shift_coordination.models.nine_o_clock,
                verbose_name="Service reminder time",
            ),
        ),
        migrations.CreateModel(
            name="ReminderDispatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=64)),
                ("skipped", models.BooleanField(default=False)),
                (
                    "notification",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="core.notification",
                    ),
                ),
                (
                    "shift",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="core.shift",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("shift", "user", "key"), name="planning_unique_reminder"
                    )
                ],
            },
        ),
    ]
