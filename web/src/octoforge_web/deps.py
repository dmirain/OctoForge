"""Dependency providers reading the composition root (app.state)."""

from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request
from octoforge_core import ConversationManager
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.cron.api import CronStore
from octoforge_core.instructions.api import InstructionService
from octoforge_core.memory.api import MemoryStore
from octoforge_core.tasks.store import TaskStore

from octoforge_web.auth import check_basic_auth
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


def get_task_store(request: Request) -> TaskStore:
    """Return the task store built at application startup."""
    return cast(TaskStore, request.app.state.task_store)


def get_instruction_service(request: Request) -> InstructionService:
    """Return the instruction service built at application startup."""
    return cast(InstructionService, request.app.state.instructions)


def get_memory_store(request: Request) -> MemoryStore:
    """Return the memory store built at application startup."""
    return cast(MemoryStore, request.app.state.memory_store)


def get_admin_read_model(request: Request) -> AdminReadModel:
    """Return the cross-user admin read model built at application startup."""
    return cast(AdminReadModel, request.app.state.admin_read_model)


def get_channel(request: Request) -> str:
    """Return the surface channel declared by the composition root."""
    return cast(str, request.app.state.channel)


def require_admin(request: Request) -> None:
    """Gate a route behind the operator credential (HTTP Basic).

    Attached to the admin router and to the dialog/cron routers: on a publicly
    reachable host the `X-User-Id` trust model is only defensible behind a
    credential.
    """
    settings = get_settings(request)
    check_basic_auth(request, settings.admin_username, settings.admin_password_hash)


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Require the trusted user id header (pre-authentication stand-in)."""
    if x_user_id is None or not x_user_id.strip():
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=MISSING_USER_ID_MESSAGE)
    return x_user_id.strip()
