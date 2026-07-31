"""Dependency providers reading the composition root (app.state)."""

from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request
from octoforge_core import ConversationManager
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.context.api import SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import ClaimRepository, DialogRepository, ExchangeRepository
from octoforge_core.instructions.api import InstructionService
from octoforge_core.secrets.api import SecretStore
from octoforge_core.tasks.store import TaskStore

from octoforge_web.auth import AuthGate
from octoforge_web.config import Settings
from octoforge_web.secret_links import SecretLinkService
from octoforge_web.telegram.invites.api import InviteStore, MemberDirectory

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


def get_admin_read_model(request: Request) -> AdminReadModel:
    """Return the cross-user admin read model built at application startup."""
    return cast(AdminReadModel, request.app.state.admin_read_model)


def get_dialog_repository(request: Request) -> DialogRepository:
    """Return the dialog repository built at application startup."""
    return cast(DialogRepository, request.app.state.dialogs)


def get_exchange_repository(request: Request) -> ExchangeRepository:
    """Return the exchange repository built at application startup."""
    return cast(ExchangeRepository, request.app.state.exchanges)


def get_claim_repository(request: Request) -> ClaimRepository:
    """Return the dialog claim repository built at application startup."""
    return cast(ClaimRepository, request.app.state.claims)


def get_telegram_members(request: Request) -> "MemberDirectory | None":
    """Return the Telegram member directory (None: bot not configured)."""
    return cast("MemberDirectory | None", request.app.state.telegram_members)


def get_telegram_invites(request: Request) -> "InviteStore | None":
    """Return the Telegram invite store (None: bot not configured)."""
    return cast("InviteStore | None", request.app.state.telegram_invites)


def get_summary_store(request: Request) -> SummaryStore:
    """Return the dialog summary store built at application startup."""
    return cast(SummaryStore, request.app.state.summary_store)


def get_channel(request: Request) -> str:
    """Return the surface channel declared by the composition root."""
    return cast(str, request.app.state.channel)


def get_secret_store(request: Request) -> SecretStore | None:
    """Return the secret store built at startup (None: feature disabled)."""
    return cast("SecretStore | None", request.app.state.secret_store)


def get_secret_links(request: Request) -> "SecretLinkService":
    """Return the one-time link service shared with the Telegram surface."""
    return cast("SecretLinkService", request.app.state.secret_links)


def get_auth_gate(request: Request) -> AuthGate:
    """Return the operator gate built at application startup."""
    return cast(AuthGate, request.app.state.auth_gate)


async def require_admin(request: Request) -> None:
    """Gate a route behind the operator credential (HTTP Basic).

    Attached to the admin router: defense in depth behind the middleware, and
    the same gate object, so a request that already authenticated in the
    middleware hits the credential cache instead of hashing a second time.
    """
    await get_auth_gate(request).authenticate(request)


def get_operator(request: Request) -> str:
    """Identify the operator for the audit trail: credential name plus client address.

    There is one operator credential, so the name alone would say little; the
    address is what distinguishes two people sharing it.
    """
    username = get_settings(request).admin_username
    client = request.client.host if request.client is not None else "unknown"
    return f"{username}@{client}"


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Require the trusted user id header (pre-authentication stand-in)."""
    if x_user_id is None or not x_user_id.strip():
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=MISSING_USER_ID_MESSAGE)
    return x_user_id.strip()
