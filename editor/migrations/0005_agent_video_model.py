from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0004_agent_image_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="video_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
