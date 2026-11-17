from pathlib import Path

from django.db import migrations, models


def forwards(apps, schema_editor):
    Workspace = apps.get_model("editor", "Workspace")
    for ws in Workspace.objects.all():
        if ws.folders_json and ws.folders_json.strip():
            continue
        p = Path(ws.path)
        name = p.name or str(p).rstrip("\\/") or ws.path
        import json
        ws.folders_json = json.dumps([{"name": name, "path": ws.path}])
        ws.save(update_fields=["folders_json"])


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspace",
            name="folders_json",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(forwards, backwards),
    ]
