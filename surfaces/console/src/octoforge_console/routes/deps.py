"""FastAPI dependency types shared by the console's resource routers."""

from typing import Annotated

from fastapi import Depends, Query
from octoforge_core import ConversationManager
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.context.api import SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import ClaimRepository, DialogRepository, ExchangeRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.instructions.api import InstructionService
from octoforge_core.params.api import UserParamStore
from octoforge_core.settings.api import SettingsStore
from octoforge_core.tariffs.api import TariffStore
from octoforge_core.tasks.store import TaskStore
from octoforge_server.deps import (
    get_activation_notifier,
    get_admin_read_model,
    get_claim_repository,
    get_conversation_manager,
    get_cron_store,
    get_dialog_repository,
    get_exchange_repository,
    get_identity_store,
    get_instruction_service,
    get_known_features,
    get_operator,
    get_settings_store,
    get_summary_store,
    get_tariff_store,
    get_task_store,
    get_user_param_store,
)
from octoforge_server.runtime_state import ActivationNotifier

ReadModelDep = Annotated[AdminReadModel, Depends(get_admin_read_model)]
CronStoreDep = Annotated[CronStore, Depends(get_cron_store)]
TaskStoreDep = Annotated[TaskStore, Depends(get_task_store)]
InstructionsDep = Annotated[InstructionService, Depends(get_instruction_service)]
ManagerDep = Annotated[ConversationManager, Depends(get_conversation_manager)]
DialogsDep = Annotated[DialogRepository, Depends(get_dialog_repository)]
OperatorDep = Annotated[str, Depends(get_operator)]
ExchangesDep = Annotated[ExchangeRepository, Depends(get_exchange_repository)]
IdentityStoreDep = Annotated[IdentityStore, Depends(get_identity_store)]
ClaimsDep = Annotated[ClaimRepository, Depends(get_claim_repository)]
SummariesDep = Annotated[SummaryStore, Depends(get_summary_store)]
UserParamsDep = Annotated[UserParamStore, Depends(get_user_param_store)]
TariffStoreDep = Annotated[TariffStore, Depends(get_tariff_store)]
KnownFeaturesDep = Annotated[frozenset[str], Depends(get_known_features)]
SettingsStoreDep = Annotated[SettingsStore, Depends(get_settings_store)]
NotifierDep = Annotated[ActivationNotifier | None, Depends(get_activation_notifier)]
LimitDep = Annotated[int | None, Query(ge=1)]
OffsetDep = Annotated[int | None, Query(ge=0)]
