import json
from pathlib import Path

from django.db import models


class Workspace(models.Model):
    """A workspace: one or more folders on disk opened in the editor."""

    name = models.CharField(max_length=120)
    path = models.CharField(max_length=500, unique=True)  # primary folder (first root)
    folders_json = models.TextField(blank=True, default="")  # [{"name","path"}, ...]
    created = models.DateTimeField(auto_now_add=True)
    last_opened = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_opened"]

    def __str__(self):
        return f"{self.name} ({self.path})"

    def get_folders(self):
        """Return [{name, path}, ...] — always at least the primary path."""
        data = []
        if self.folders_json.strip():
            try:
                raw = json.loads(self.folders_json)
                if isinstance(raw, list):
                    data = [
                        {"name": str(f["name"]), "path": str(f["path"])}
                        for f in raw
                        if isinstance(f, dict) and f.get("path")
                    ]
            except (ValueError, TypeError, KeyError):
                data = []
        if not data and self.path:
            data = [{"name": _folder_label(self.path), "path": self.path}]
        return data

    def set_folders(self, folders):
        if not folders:
            raise ValueError("Workspace needs at least one folder.")
        cleaned = []
        seen_paths = set()
        used_names = set()
        for f in folders:
            path = str(Path(f["path"]).resolve())
            if path in seen_paths:
                continue
            seen_paths.add(path)
            base = str(f.get("name") or _folder_label(path)).strip() or path
            name = base
            n = 2
            while name in used_names:
                name = f"{base} ({n})"
                n += 1
            used_names.add(name)
            cleaned.append({"name": name, "path": path})
        self.folders_json = json.dumps(cleaned)
        self.path = self._unique_path(cleaned[0]["path"])

    def _unique_path(self, base):
        """Keep Workspace.path unique after the first folder changes.

        Opening a folder looks the workspace up by its real path. If that path
        already belongs to another workspace, store a distinct key so this row
        can still be saved.
        """
        def taken(candidate):
            qs = Workspace.objects.filter(path=candidate)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            return qs.exists()

        if not taken(base):
            return base
        n = 2
        while taken(f"{base}#{n}"):
            n += 1
        return f"{base}#{n}"

    def add_folder(self, path, name=None):
        path = str(Path(path).resolve())
        folders = self.get_folders()
        if any(f["path"] == path for f in folders):
            return folders  # already present
        folders.append({"name": name or _folder_label(path), "path": path})
        self.set_folders(folders)
        return self.get_folders()

    def remove_folder(self, path=None, name=None):
        before = self.get_folders()
        if len(before) <= 1:
            raise ValueError("Cannot remove the last folder from the workspace.")
        if path:
            path = str(Path(path).resolve())
            folders = [f for f in before if f["path"] != path]
        elif name:
            folders = [f for f in before if f["name"] != name]
        else:
            raise ValueError("Specify path or name.")
        if len(folders) == len(before):
            raise ValueError("Folder not found in workspace.")
        self.set_folders(folders)
        return self.get_folders()


def _folder_label(path):
    p = Path(path)
    return p.name or str(p).rstrip("\\/") or str(path)


class Agent(models.Model):
    """An external agent / model endpoint PSEditor can talk to."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    WEBHOOK = "webhook"
    CLI = "cli"
    PROVIDERS = [
        (OPENAI, "OpenAI-compatible API (OpenAI, OpenRouter, Ollama, LM Studio, ...)"),
        (ANTHROPIC, "Anthropic Messages API (Claude)"),
        (WEBHOOK, "Custom HTTP agent (webhook)"),
        (CLI, "Command-line agent (claude, codex, aider, ...)"),
    ]

    name = models.CharField(max_length=120, unique=True)
    provider = models.CharField(max_length=20, choices=PROVIDERS, default=OPENAI)
    base_url = models.CharField(
        max_length=500, blank=True,
        help_text="API base URL (openai/anthropic) or full webhook URL.",
    )
    api_key = models.CharField(max_length=500, blank=True)
    model = models.CharField(max_length=200, blank=True)
    image_model = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Model used by generate_image. Blank uses the built-in default.",
    )
    video_model = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Model used by generate_video. Blank uses the short-clip default.",
    )
    command = models.CharField(
        max_length=1000, blank=True,
        help_text="CLI template. Use {prompt} to inject the prompt, otherwise it is sent on stdin.",
    )
    system_prompt = models.TextField(blank=True)
    temperature = models.FloatField(null=True, blank=True)
    extra_headers = models.TextField(blank=True, help_text="JSON object of extra HTTP headers.")
    is_default = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def headers(self):
        if not self.extra_headers.strip():
            return {}
        try:
            data = json.loads(self.extra_headers)
            return {str(k): str(v) for k, v in data.items()}
        except (ValueError, AttributeError):
            return {}

    def to_dict(self, include_secret=False):
        data = {
            "id": self.pk,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "image_model": self.image_model,
            "video_model": self.video_model,
            "command": self.command,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "extra_headers": self.extra_headers,
            "is_default": self.is_default,
            "has_key": bool(self.api_key),
        }
        if include_secret:
            data["api_key"] = self.api_key
        return data


class ChatSession(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="sessions")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=200, default="New chat")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]

    def __str__(self):
        return self.title


class ChatMessage(models.Model):
    """One message in a conversation.

    ``payload`` stores the provider-neutral message as JSON:
      {"role": "user"|"assistant"|"tool", "content": str,
       "tool_calls": [{"id","name","arguments"}], "tool_call_id": str, "name": str,
       "attachments": [{"name","mime","kind"}], "images": [{"name","mime","data"}]}
    ``checkpoint`` stores the original content of files touched by the agent
    during that run so the changes can be reverted.
    """

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20)
    payload = models.TextField()
    checkpoint = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def data(self):
        return json.loads(self.payload)
