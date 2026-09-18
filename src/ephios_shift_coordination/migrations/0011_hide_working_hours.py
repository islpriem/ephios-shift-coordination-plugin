from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ephios_shift_coordination", "0010_next_period_reminder"),
    ]

    operations = [
        migrations.AddField(
            model_name="planningsettings",
            name="hide_working_hours",
            field=models.BooleanField(default=False, verbose_name="Hide the working hours"),
        ),
    ]
