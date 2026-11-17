from django.contrib import admin

from .models import Agent, ChatMessage, ChatSession, Workspace

admin.site.register(Workspace)
admin.site.register(Agent)
admin.site.register(ChatSession)
admin.site.register(ChatMessage)
