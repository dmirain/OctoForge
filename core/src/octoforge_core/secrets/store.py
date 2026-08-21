"""Encrypted SQL persistence for per-user secrets."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.secrets._commands import store_secret
from octoforge_core.secrets._queries import find_secret_row
from octoforge_core.secrets._rows import (
    placements_from_column,
    to_info,
    transform_from_column,
)
from octoforge_core.secrets._values import SecretDecryptionError, SecretValueCipher
from octoforge_core.secrets.models import SecretRow
from octoforge_core.secrets.policy import host_matches, normalize_code, normalize_host
from octoforge_core.secrets.transforms import apply_transform
from octoforge_core.secrets.types import (
    ResolvedSecret,
    SecretHostMismatchError,
    SecretInfo,
    SecretNotFoundError,
    SecretWrite,
)
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

HOST_MISMATCH_MESSAGE = (
    "secret '{code}' is bound to host '{allowed}' and cannot be sent to '{host}'"
)


class SqlAlchemySecretStore:
    """Encrypt values at rest and expose only metadata or host-bound resolution."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], key: str) -> None:
        self._values = SecretValueCipher(key)
        self._session_factory = session_factory

    async def put(self, request: SecretWrite) -> SecretInfo:
        return await store_secret(self._session_factory, self._values, request)

    async def list(self, user_id: str) -> list[SecretInfo]:
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(
                    select(SecretRow)
                    .where(SecretRow.user_id == user_id)
                    .order_by(SecretRow.created_at.desc(), SecretRow.code)
                )
            ).all()
            return [to_info(row) for row in rows]

    async def delete(self, user_id: str, code: str) -> None:
        normalized = normalize_code(code)
        async with write_session(self._session_factory) as session:
            row = await find_secret_row(session, user_id, normalized)
            if row is None:
                raise SecretNotFoundError(normalized)
            await session.delete(row)

    async def resolve(self, user_id: str, code: str, host: str) -> ResolvedSecret:
        normalized = normalize_code(code)
        target = normalize_host(host)
        async with write_session(self._session_factory) as session:
            row = await find_secret_row(session, user_id, normalized)
            if row is None:
                raise SecretNotFoundError(normalized)
            _ensure_host(row, target)
            plain = self._decrypt(row)
            row.last_used_at = utc_now()
            transform = transform_from_column(row.transform)
            return ResolvedSecret(
                value=apply_transform(plain, transform),
                plain=plain,
                placements=placements_from_column(row.placements),
            )

    def _decrypt(self, row: SecretRow) -> str:
        try:
            return self._values.decrypt(row.ciphertext)
        except SecretDecryptionError:
            logger.error(
                "secret undecryptable (OF_SECRETS_KEY changed?): user=%s code=%s",
                row.user_id,
                row.code,
            )
            raise SecretNotFoundError(row.code) from None


def _ensure_host(row: SecretRow, target: str) -> None:
    if host_matches(row.allowed_host, target):
        return
    raise SecretHostMismatchError(
        HOST_MISMATCH_MESSAGE.format(code=row.code, allowed=row.allowed_host, host=target)
    )
