"""Persistence owned by the Telegram surface."""

from __future__ import annotations

from dataclasses import dataclass

from octoforge_core import create_engine, create_session_factory
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.drafts import SqlAlchemyDraftStore
from octoforge_telegram.invites.store import (
    SqlAlchemyInviteStore,
    SqlAlchemyMemberDirectory,
    SqlAlchemyReferralStore,
)
from octoforge_telegram.schema import TelegramSurfaceBase
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class TelegramStores:
    """The Telegram surface's isolated database."""

    invites: SqlAlchemyInviteStore
    referrals: SqlAlchemyReferralStore
    directory: SqlAlchemyMemberDirectory
    drafts: SqlAlchemyDraftStore
    engine: AsyncEngine

    async def aclose(self) -> None:
        await self.engine.dispose()


async def build_telegram_stores(settings: TelegramSettings) -> TelegramStores | None:
    """Build Telegram persistence only when the surface is enabled."""
    if not settings.telegram_bot_token:
        return None
    engine = create_engine(settings.telegram_database_url)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    sessions = create_session_factory(engine)
    return TelegramStores(
        invites=SqlAlchemyInviteStore(
            sessions,
            ttl_seconds=settings.telegram_invite_ttl_seconds,
        ),
        referrals=SqlAlchemyReferralStore(sessions),
        directory=SqlAlchemyMemberDirectory(sessions),
        drafts=SqlAlchemyDraftStore(sessions),
        engine=engine,
    )
