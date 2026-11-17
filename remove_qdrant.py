import django
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pseditor.settings")
django.setup()

from editor.models import Workspace

# Workspace #21 has both QdrantFileReader_Portable and pseditor.
# Workspace #20 already has pseditor alone, so #21 would become a duplicate
# if we removed Qdrant from it. Just delete #21 entirely.
w = Workspace.objects.get(pk=21)
print("Deleting workspace:", w.name, w.path, w.folders_json)
w.delete()

print("Remaining workspaces:")
for x in Workspace.objects.all():
    print(x.pk, x.name, x.path, x.folders_json)
