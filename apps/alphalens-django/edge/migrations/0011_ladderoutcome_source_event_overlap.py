# Event lane (epic #1293, #1297): the candidate SOURCE lane and the same-day
# overlap flag travel with the outcome record so the thematic aggregates can
# exclude the lane's rows (never pooled) and the SPA can facet on it.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("edge", "0010_ladderoutcome_captured_tp_count_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ladderoutcome",
            name="source",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="ladderoutcome",
            name="event_overlap",
            field=models.BooleanField(default=False),
        ),
    ]
