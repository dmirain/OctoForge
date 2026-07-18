"""Dependency providers reading the composition root (app.state)."""

from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request
from octoforge_core import ConversationManager
from octoforge_core.cron.api import CronStore

from octoforge_web.config import Settings

MISSING_USER_ID_MESSAGE = "X-User-Id header is required"


def get_settings(request: Request) -> Settings:
    """Return the application settings."""
    return cast(Settings, request.app.state.settings)


def get_conversation_manager(request: Request) -> ConversationManager:
    """Return the conversation manager built at application startup."""
    return cast(ConversationManager, request.app.state.conversation_manager)


def get_cron_store(request: Request) -> CronStore:
    """Return the cron job store built at application startup."""
    return cast(CronStore, request.app.state.cron_store)


def get_channel(request: Request) -> str:
    """Return the surface channel declared by the composition root."""
    return cast(str, request.app.state.channel)


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Require the trusted user id header (pre-authentication stand-in)."""
    if x_user_id is None or not x_user_id.strip():
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=MISSING_USER_ID_MESSAGE)
    return x_user_id.strip()
