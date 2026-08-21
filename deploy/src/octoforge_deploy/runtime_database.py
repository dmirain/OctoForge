"""Database-owned portion of the deployment graph."""

from __future__ import annotations

from dataclasses import dataclass

from octoforge_core import (
    ManagerStores,
    SqlAlchemyTaskStore,
    UnitOfWork,
    build_limit_service,
    create_engine,
    create_session_factory,
)
from octoforge_core.composition import LexicalBackend
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.identity.service import AccessService
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.params.store import SqlAlchemyUserParamStore
from octoforge_core.secrets.api import SecretStore
from octoforge_core.secrets.link_store import SqlAlchemySecretFormLinkStore
from octoforge_core.secrets.store import SqlAlchemySecretStore
from octoforge_core.settings.api import max_active_users
from octoforge_core.settings.store import SqlAlchemySettingsStore
from octoforge_core.tariffs.service import LimitService
from octoforge_core.tariffs.store import SqlAlchemyTariffStore, SqlAlchemyUsageMeter
from octoforge_server.config import Settings
from octoforge_server.secret_links import SecretLinkService
from octoforge_telegram.config import TelegramSettings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_deploy.runtime_database_probe import bootstrap, probe_backends, sweep_retention
from octoforge_deploy.runtime_telegram_storage import TelegramStores, build_telegram_stores


@dataclass(frozen=True, slots=True)
class CoreStores:
    manager: ManagerStores
    identity: SqlAlchemyIdentityStore
    cron: SqlAlchemyCronStore
    secrets: SecretStore | None
    user_params: SqlAlchemyUserParamStore
    tariff: SqlAlchemyTariffStore
    usage: SqlAlchemyUsageMeter
    settings: SqlAlchemySettingsStore
    secret_links: SecretLinkService
    limits: LimitService
    access: AccessService


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    extensions: frozenset[str]
    lexical: LexicalBackend
    stores: CoreStores
    telegram: TelegramStores | None

    async def aclose(self) -> None:
        await self.engine.dispose()
        if self.telegram is not None:
            await self.telegram.aclose()


async def build_database_runtime(
    settings: Settings,
    telegram_settings: TelegramSettings,
) -> DatabaseRuntime:
    engine = create_engine(settings.database_url)
    await bootstrap(engine)
    extensions, lexical = await probe_backends(engine)
    sessions = create_session_factory(engine)
    await sweep_retention(settings, sessions)
    return DatabaseRuntime(
        engine,
        sessions,
        extensions,
        lexical,
        _build_stores(settings, sessions),
        await build_telegram_stores(telegram_settings),
    )


def _build_stores(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
) -> CoreStores:
    manager = ManagerStores(
        dialogs=SqlAlchemyDialogRepository(sessions),
        messages=SqlAlchemyMessageRepository(sessions),
        tasks=SqlAlchemyTaskStore(sessions),
        exchanges=SqlAlchemyExchangeRepository(sessions),
        claims=SqlAlchemyClaimRepository(sessions),
        uow=UnitOfWork(sessions),
    )
    tariff, usage = SqlAlchemyTariffStore(sessions), SqlAlchemyUsageMeter(sessions)
    settings_store = SqlAlchemySettingsStore(sessions)
    identity = SqlAlchemyIdentityStore(sessions)
    key = settings.secrets_key
    secrets = SqlAlchemySecretStore(sessions, key) if key else None
    return CoreStores(
        manager,
        identity,
        SqlAlchemyCronStore(sessions),
        secrets,
        SqlAlchemyUserParamStore(sessions),
        tariff,
        usage,
        settings_store,
        SecretLinkService(settings.secrets_key, codes=SqlAlchemySecretFormLinkStore(sessions)),
        build_limit_service(tariff, usage),
        AccessService(identity, lambda: max_active_users(settings_store)),
    )
