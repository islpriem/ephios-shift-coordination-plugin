import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_alter_userprofile_disabled_notifications"),
        ("ephios_shift_coordination", "0005_observers_and_suggestions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Assembly",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("agenda", models.TextField(blank=True, max_length=8000, verbose_name="Agenda")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("invited_at", models.DateTimeField(null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assembly",
                        to="core.event",
                    ),
                ),
            ],
        ),
    ]
