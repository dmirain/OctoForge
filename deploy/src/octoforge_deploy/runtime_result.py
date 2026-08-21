"""Projection of the running graph onto the server runtime port."""

from __future__ import annotations

from octoforge_core.admin.store import SqlAlchemyAdminStore
from octoforge_core.tariffs.api import CORE_FEATURES
from octoforge_server.channels import WEB_CHANNEL
from octoforge_server.runtime_state import Runtime
from octoforge_telegram.client import TelegramBotClient
from octoforge_telegram.notifier import TelegramActivationNotifier

from octoforge_deploy.runtime_state import RuntimeGraph
from octoforge_deploy.runtime_surfaces import served_channels, surface_state


def build_runtime_result(graph: RuntimeGraph) -> Runtime:
    database, stores = graph.database, graph.database.stores
    manager_stores = stores.manager
    return Runtime(
        settings=graph.settings,
        conversation_manager=graph.manager,
        channel=WEB_CHANNEL,
        cron_store=stores.cron,
        session_factory=database.sessions,
        task_store=manager_stores.tasks,
        instructions=graph.knowledge.instructions,
        admin_read_model=SqlAlchemyAdminStore(database.sessions),
        secret_store=stores.secrets,
        secret_links=stores.secret_links,
        user_params=stores.user_params,
        tariff_store=stores.tariff,
        usage_meter=stores.usage,
        limit_gate=stores.limits,
        known_features=CORE_FEATURES,
        dialogs=manager_stores.dialogs,
        summary_store=graph.knowledge.summaries,
        exchanges=manager_stores.exchanges,
        claims=manager_stores.claims,
        identity_store=stores.identity,
        settings_store=stores.settings,
        access=stores.access,
        media_service=graph.media.service,
        channels=served_channels(graph.surfaces),
        activation_notifier=_activation_notifier(graph),
        surface_state=surface_state(database.telegram),
    )


def _activation_notifier(graph: RuntimeGraph) -> TelegramActivationNotifier | None:
    token = graph.telegram.telegram_bot_token
    if not token:
        return None
    return TelegramActivationNotifier(
        TelegramBotClient(graph.clients.outbound, token),
        graph.database.stores.identity,
    )
