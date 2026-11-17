from django.db import migrations

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "anthropic/claude-sonnet-5.5"


def add_openrouter_agent(apps, schema_editor):
    Agent = apps.get_model("editor", "Agent")
    if Agent.objects.exists():
        return
    Agent.objects.create(
        name="OpenRouter", provider="openai", base_url=OPENROUTER_URL,
        model=OPENROUTER_MODEL, is_default=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0002_workspace_folders"),
    ]

    operations = [
        migrations.RunPython(add_openrouter_agent, migrations.RunPython.noop),
    ]
