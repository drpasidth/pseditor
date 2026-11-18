from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0003_default_openrouter_agent"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="image_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
