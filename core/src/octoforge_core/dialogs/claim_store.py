"""SQL ownership claims for one actor process per dialog."""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.dialogs.claim_statement import (
    FIRST_GENERATION,
    ClaimWrite,
    claim_upsert,
)
from octoforge_core.dialogs.models import DialogClaimRow
from octoforge_core.dialogs.types import DialogClaim, DialogClaimList
from octoforge_core.time import utc_now


class SqlAlchemyClaimRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def claim(self, dialog_id: str, owner: str) -> DialogClaim:
        now = utc_now()
        async with write_session(self._sessions) as session:
            generation = await session.scalar(
                claim_upsert(session, ClaimWrite(dialog_id, owner, now))
            )
        return DialogClaim(dialog_id, owner, int(generation or FIRST_GENERATION), now)

    async def heartbeat(self, claims: DialogClaimList) -> frozenset[str]:
        if not claims:
            return frozenset()
        held = {claim.dialog_id: claim for claim in claims}
        async with write_session(self._sessions) as session:
            rows = (
                await session.execute(
                    select(
                        DialogClaimRow.dialog_id,
                        DialogClaimRow.owner,
                        DialogClaimRow.generation,
                    ).where(DialogClaimRow.dialog_id.in_(held))
                )
            ).all()
            kept = frozenset(
                dialog_id
                for dialog_id, owner, generation in rows
                if held[dialog_id].owner == owner and held[dialog_id].generation == generation
            )
            if kept:
                await session.execute(
                    update(DialogClaimRow)
                    .where(DialogClaimRow.dialog_id.in_(kept))
                    .values(heartbeat_at=utc_now())
                )
            return kept

    async def release(self, dialog_id: str, owner: str, generation: int) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(
                delete(DialogClaimRow).where(
                    DialogClaimRow.dialog_id == dialog_id,
                    DialogClaimRow.owner == owner,
                    DialogClaimRow.generation == generation,
                )
            )

    async def held_elsewhere(
        self,
        dialog_ids: frozenset[str],
        owner: str,
        stale_before: datetime,
    ) -> frozenset[str]:
        if not dialog_ids:
            return frozenset()
        async with read_session(self._sessions) as session:
            rows = await session.scalars(
                select(DialogClaimRow.dialog_id).where(
                    DialogClaimRow.dialog_id.in_(dialog_ids),
                    DialogClaimRow.owner != owner,
                    DialogClaimRow.heartbeat_at >= stale_before,
                )
            )
            return frozenset(rows.all())

    async def current_generation(self, dialog_id: str) -> int | None:
        async with read_session(self._sessions) as session:
            generation = await session.scalar(
                select(DialogClaimRow.generation).where(DialogClaimRow.dialog_id == dialog_id)
            )
            return int(generation) if generation is not None else None

    async def delete_for_dialog(self, dialog_id: str) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(
                delete(DialogClaimRow).where(DialogClaimRow.dialog_id == dialog_id)
            )
