"""FastAPI dependencies that expose objects assembled in app.state."""

from typing import cast

from fastapi import Request
from octoforge_core import ConversationManager
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.context.api import SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import ClaimRepository, DialogRepository, ExchangeRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.identity.service import AccessService
from octoforge_core.instructions.api import InstructionService
from octoforge_core.media.service import MediaService
from octoforge_core.params.api import UserParamStore
from octoforge_core.secrets.api import SecretStore
from octoforge_core.settings.api import SettingsStore
from octoforge_core.tariffs.api import LimitGate, TariffStore, UsageMeter
from octoforge_core.tasks.store import TaskStore

from octoforge_server.auth import AuthGate
from octoforge_server.config import Settings
from octoforge_server.runtime_state import ActivationNotifier
from octoforge_server.secret_links import SecretLinkService


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_conversation_manager(request: Request) -> ConversationManager:
    return cast(ConversationManager, request.app.state.conversation_manager)


def get_cron_store(request: Request) -> CronStore:
    return cast(CronStore, request.app.state.cron_store)


def get_task_store(request: Request) -> TaskStore:
    return cast(TaskStore, request.app.state.task_store)


def get_instruction_service(request: Request) -> InstructionService:
    return cast(InstructionService, request.app.state.instructions)


def get_admin_read_model(request: Request) -> AdminReadModel:
    return cast(AdminReadModel, request.app.state.admin_read_model)


def get_dialog_repository(request: Request) -> DialogRepository:
    return cast(DialogRepository, request.app.state.dialogs)


def get_exchange_repository(request: Request) -> ExchangeRepository:
    return cast(ExchangeRepository, request.app.state.exchanges)


def get_claim_repository(request: Request) -> ClaimRepository:
    return cast(ClaimRepository, request.app.state.claims)


def get_summary_store(request: Request) -> SummaryStore:
    return cast(SummaryStore, request.app.state.summary_store)


def get_secret_store(request: Request) -> SecretStore | None:
    return cast("SecretStore | None", request.app.state.secret_store)


def get_secret_links(request: Request) -> SecretLinkService:
    return cast(SecretLinkService, request.app.state.secret_links)


def get_user_param_store(request: Request) -> UserParamStore:
    return cast(UserParamStore, request.app.state.user_params)


def get_tariff_store(request: Request) -> TariffStore:
    return cast(TariffStore, request.app.state.tariff_store)


def get_usage_meter(request: Request) -> UsageMeter:
    return cast(UsageMeter, request.app.state.usage_meter)


def get_limit_gate(request: Request) -> LimitGate:
    return cast(LimitGate, request.app.state.limit_gate)


def get_known_features(request: Request) -> frozenset[str]:
    return cast(frozenset[str], request.app.state.known_features)


def get_auth_gate(request: Request) -> AuthGate:
    return cast(AuthGate, request.app.state.auth_gate)


def get_identity_store(request: Request) -> IdentityStore:
    return cast(IdentityStore, request.app.state.identity_store)


def get_settings_store(request: Request) -> SettingsStore:
    return cast(SettingsStore, request.app.state.settings_store)


def get_access_service(request: Request) -> AccessService:
    return cast(AccessService, request.app.state.access)


def get_media_service(request: Request) -> MediaService:
    return cast(MediaService, request.app.state.media_service)


def get_activation_notifier(request: Request) -> ActivationNotifier | None:
    return cast("ActivationNotifier | None", request.app.state.activation_notifier)
