"""Dependency providers reading the composition root (app.state)."""

from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request
from octoforge_core import ConversationManager
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.context.api import SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import ClaimRepository, DialogRepository, ExchangeRepository
from octoforge_core.identity.api import IdentityStore, UserStatus
from octoforge_core.identity.service import AccessService
from octoforge_core.instructions.api import InstructionService
from octoforge_core.params.api import UserParamStore
from octoforge_core.secrets.api import SecretStore
from octoforge_core.settings.api import SettingsStore
from octoforge_core.tariffs.api import LimitGate, TariffStore, UsageMeter
from octoforge_core.tasks.store import TaskStore

from octoforge_server.auth import AuthGate
from octoforge_server.config import Settings
from octoforge_server.runtime_state import ActivationNotifier
from octoforge_server.secret_links import SecretLinkService

CHANNEL_HEADER = "X-Channel"
UNKNOWN_CHANNEL_MESSAGE = "unknown channel: {channel}"
MISSING_USER_ID_MESSAGE = "X-User-Id header is required"
# neither message names a queue position: it is not a promise the
# installation can keep
ACCESS_WAITING_MESSAGE = "registration is pending: no free slots right now"
ACCESS_BANNED_MESSAGE = "access is closed"


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


def get_summary_store(request: Request) -> SummaryStore:
    """Return the dialog summary store built at application startup."""
    return cast(SummaryStore, request.app.state.summary_store)


def _known_channels(request: Request) -> frozenset[str]:
    """Every channel this deployment serves, as its surfaces declared them."""
    return cast(frozenset[str], request.app.state.channels)


def get_channel(request: Request) -> str:
    """Return the channel this request addresses.

    The header wins, the composition root's own channel is the default. That
    default is what keeps the chat UI (and every existing client) working
    without knowing the concept exists.

    A per-request channel is what lets one process serve several surfaces:
    without it a deployment needs a separate fleet per channel, which defeats
    balancing users across a single one. Unknown values are refused — an
    accepted typo would silently open a dialog nobody ever reads.
    """
    declared = cast(str, request.app.state.channel)
    channel = request.headers.get(CHANNEL_HEADER)
    if channel is None:
        return declared
    if channel not in _known_channels(request):
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=UNKNOWN_CHANNEL_MESSAGE.format(channel=channel),
        )
    return channel


def get_secret_store(request: Request) -> SecretStore | None:
    """Return the secret store built at startup (None: feature disabled)."""
    return cast("SecretStore | None", request.app.state.secret_store)


def get_secret_links(request: Request) -> "SecretLinkService":
    """Return the one-time link service shared with the Telegram surface."""
    return cast("SecretLinkService", request.app.state.secret_links)


def get_user_param_store(request: Request) -> UserParamStore:
    """Return the per-user params store built at startup."""
    return cast(UserParamStore, request.app.state.user_params)


def get_tariff_store(request: Request) -> TariffStore:
    """Return the tariff catalog store built at startup."""
    return cast(TariffStore, request.app.state.tariff_store)


def get_usage_meter(request: Request) -> UsageMeter:
    """Return the usage ledger built at startup."""
    return cast(UsageMeter, request.app.state.usage_meter)


def get_limit_gate(request: Request) -> LimitGate:
    """Return the tariff limit gate built at startup."""
    return cast(LimitGate, request.app.state.limit_gate)


def get_known_features(request: Request) -> frozenset[str]:
    """Return the feature vocabulary this assembly declared at startup."""
    return cast(frozenset[str], request.app.state.known_features)


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


def get_external_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """What the calling surface calls this user.

    Not a person: surfaces number their users independently, so `42` on one is
    not `42` on another. Turning it into a person is `get_user_id`'s job.
    """
    if x_user_id is None or not x_user_id.strip():
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=MISSING_USER_ID_MESSAGE)
    return x_user_id.strip()


def get_identity_store(request: Request) -> IdentityStore:
    """Return the identity store built at application startup."""
    return cast(IdentityStore, request.app.state.identity_store)


def get_settings_store(request: Request) -> SettingsStore:
    """Return the operator-settings store built at startup."""
    return cast(SettingsStore, request.app.state.settings_store)


def get_access_service(request: Request) -> AccessService:
    """Return the admission gate built at startup."""
    return cast(AccessService, request.app.state.access)


def get_activation_notifier(request: Request) -> "ActivationNotifier | None":
    """How to tell a person their access was opened (None: nobody can reach them)."""
    return cast("ActivationNotifier | None", request.app.state.activation_notifier)


async def get_user_id(
    request: Request,
    external_id: Annotated[str, Depends(get_external_id)],
    channel: Annotated[str, Depends(get_channel)],
) -> str:
    """The person behind (surface, external id), minted on first contact.

    Resolution lives here rather than in each surface: a surface knows what it
    calls someone and nothing more, and the actor below wants a person. Who
    may talk at all was already decided — by the invite gate, or by the
    credential in front of this service — so an unknown account arriving here
    is somebody new. What their *status* allows is decided right here: a
    banned or still-waiting person gets 403, never a dialog.
    """
    person = await get_identity_store(request).resolve_or_create(channel, external_id)
    status = await get_access_service(request).admit(person)
    if status is UserStatus.BANNED:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=ACCESS_BANNED_MESSAGE)
    if status is not UserStatus.ACTIVE:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=ACCESS_WAITING_MESSAGE)
    return person
