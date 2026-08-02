"""SQLAlchemy + Fernet implementation of the SecretStore port.

Values are encrypted at rest (Fernet, AES-128-CBC + HMAC): neither the
database file nor a `pg_dump` backup contains plaintext. The key comes from
the composition root (`OF_SECRETS_KEY` in the web app); a wrong key at
decryption surfaces as SecretNotFoundError rather than a crash — to every
consumer an undecryptable secret IS a missing one, and the log tells the
operator why.
"""

import logging
import uuid

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.secrets.api import (
    InvalidSecretError,
    SecretHostMismatchError,
    SecretInfo,
    SecretNotFoundError,
    normalize_code,
    normalize_host,
)
from octoforge_core.secrets.models import SecretRow
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

MAX_VALUE_CHARS = 8192
HOST_MISMATCH_MESSAGE = (
    "secret '{code}' is bound to host '{allowed}' and cannot be sent to '{host}'"
)


class SqlAlchemySecretStore:
    """Encrypted SQL persistence for per-user secrets."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], key: str) -> None:
        # a malformed key fails here, at composition time — not on first use
        self._fernet = Fernet(key)
        self._session_factory = session_factory

    async def put(self, user_id: str, code: str, value: str, allowed_host: str) -> SecretInfo:
        """Store or replace the user's secret under `code`, bound to `allowed_host`."""
        code = normalize_code(code)
        host = normalize_host(allowed_host)
        if not value or len(value) > MAX_VALUE_CHARS:
            raise InvalidSecretError(f"secret value must be 1..{MAX_VALUE_CHARS} characters")
        if not _is_header_safe(value):
            # the value travels as an HTTP header: control characters would be
            # a header-injection vector, non-ASCII is unencodable there anyway
            raise InvalidSecretError(
                "secret value must be printable ASCII (it is sent as an HTTP header)"
            )
        ciphertext = self._fernet.encrypt(value.encode()).decode()
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, user_id, code)
            if row is None:
                row = SecretRow(
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    code=code,
                    ciphertext=ciphertext,
                    allowed_host=host,
                )
                session.add(row)
            else:
                row.ciphertext = ciphertext
                row.allowed_host = host
                row.created_at = utc_now()  # a replaced secret is a new secret
                row.last_used_at = None
            await session.flush()
            return _to_info(row)

    async def list(self, user_id: str) -> list[SecretInfo]:
        """Return the user's secrets metadata, newest first. Never the values."""
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(
                    select(SecretRow)
                    .where(SecretRow.user_id == user_id)
                    .order_by(SecretRow.created_at.desc(), SecretRow.code)
                )
            ).all()
            return [_to_info(row) for row in rows]

    async def delete(self, user_id: str, code: str) -> None:
        """Delete the user's secret by code; raise `SecretNotFoundError`."""
        code = normalize_code(code)
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, user_id, code)
            if row is None:
                raise SecretNotFoundError(code)
            await session.delete(row)

    async def resolve(self, user_id: str, code: str, host: str) -> str:
        """Return the decrypted value for a request going to `host`."""
        code = normalize_code(code)
        target = normalize_host(host)
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, user_id, code)
            if row is None:
                raise SecretNotFoundError(code)
            if row.allowed_host != target:
                raise SecretHostMismatchError(
                    HOST_MISMATCH_MESSAGE.format(code=code, allowed=row.allowed_host, host=target)
                )
            try:
                value = self._fernet.decrypt(row.ciphertext.encode()).decode()
            except InvalidToken:
                # the key changed under stored data: to the caller the secret
                # is gone (re-enter it); the log explains the real cause
                logger.error(
                    "secret undecryptable (OF_SECRETS_KEY changed?): user=%s code=%s",
                    user_id,
                    code,
                )
                raise SecretNotFoundError(code) from None
            row.last_used_at = utc_now()
            return value


def _is_header_safe(value: str) -> bool:
    return all(" " <= char <= "~" for char in value)


async def _find_row(session: AsyncSession, user_id: str, code: str) -> SecretRow | None:
    result = await session.scalars(
        select(SecretRow).where(SecretRow.user_id == user_id, SecretRow.code == code)
    )
    return result.first()


def _to_info(row: SecretRow) -> SecretInfo:
    return SecretInfo(
        code=row.code,
        allowed_host=row.allowed_host,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )
