"""Dependency providers reading the composition root (app.state)."""

from typing import cast

from fastapi import Request
from octoforge_core import ConversationManager

from octoforge_web.config import Settings


def get_settings(request: Request) -> Settings:
    """Return the application settings."""
    return cast(Settings, request.app.state.settings)


def get_conversation_manager(request: Request) -> ConversationManager:
    """Return the conversation manager built at application startup."""
    return cast(ConversationManager, request.app.state.conversation_manager)
