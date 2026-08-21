"""SQL command for validating, encrypting and storing one secret."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.secrets._queries import find_secret_row
from octoforge_core.secrets._rows import placements_to_column, to_info
from octoforge_core.secrets._values import PreparedSecret, SecretValueCipher
from octoforge_core.secrets.models import SecretRow
from octoforge_core.secrets.types import SecretInfo, SecretWrite
from octoforge_core.time import utc_now


async def store_secret(
    session_factory: async_sessionmaker[AsyncSession],
    values: SecretValueCipher,
    request: SecretWrite,
) -> SecretInfo:
    prepared = values.prepare(request)
    async with write_session(session_factory) as session:
        row = await find_secret_row(session, prepared.user_id, prepared.code)
        if row is None:
            row = _new_row(prepared)
            session.add(row)
        else:
            _replace_value(row, prepared)
        row.description = prepared.description
        row.placements = placements_to_column(prepared.placements)
        row.transform = prepared.transform.value if prepared.transform is not None else None
        await session.flush()
        return to_info(row)


def _new_row(secret: PreparedSecret) -> SecretRow:
    return SecretRow(
        id=uuid.uuid4().hex,
        user_id=secret.user_id,
        code=secret.code,
        ciphertext=secret.ciphertext,
        allowed_host=secret.allowed_host,
    )


def _replace_value(row: SecretRow, secret: PreparedSecret) -> None:
    row.ciphertext = secret.ciphertext
    row.allowed_host = secret.allowed_host
    row.created_at = utc_now()
    row.last_used_at = None
