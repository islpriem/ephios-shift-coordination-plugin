import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ephios_shift_coordination", "0009_public_duty_api"),
    ]

    operations = [
        migrations.AddField(
            model_name="planningperiod",
            name="next_reminder_on",
            field=models.DateField(null=True),
        ),
        migrations.AddField(
            model_name="planningsettings",
            name="next_period_weeks",
            field=models.PositiveSmallIntegerField(
                default=2,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="Remind about the next period this many weeks before the end",
            ),
        ),
    ]
