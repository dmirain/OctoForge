"""Dialect-specific upsert that takes or bumps one dialog claim."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from octoforge_core.dialogs.models import DialogClaimRow

FIRST_GENERATION = 1


@dataclass(frozen=True, slots=True)
class ClaimWrite:
    dialog_id: str
    owner: str
    now: datetime


def claim_upsert(
    session: AsyncSession,
    request: ClaimWrite,
) -> ReturningInsert[tuple[int]]:
    insert_for_dialect = (
        postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    )
    statement = insert_for_dialect(DialogClaimRow).values(
        dialog_id=request.dialog_id,
        owner=request.owner,
        generation=FIRST_GENERATION,
        heartbeat_at=request.now,
    )
    return statement.on_conflict_do_update(
        index_elements=[DialogClaimRow.dialog_id],
        set_={
            "owner": statement.excluded.owner,
            "generation": DialogClaimRow.generation + 1,
            "heartbeat_at": statement.excluded.heartbeat_at,
        },
    ).returning(DialogClaimRow.generation)
