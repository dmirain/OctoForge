"""SQL storage of the secrets-form capability codes.

Why a table at all, when the tokens it replaces were self-contained: the
agent has to put the link into a chat message, and a ~700-character opaque
token is something a model imitates rather than copies (2026-08-04: two
invented tokens, one of them a 30 000-character repetition loop). A short
code is copyable — so the payload moves here, where it is also tamper-proof
by construction rather than by encryption.

The stateless account tokens minted by surfaces stay as they are: an
ingestion node runs outside this service and has no database to write to.
"""

import json
import secrets as random_secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.secrets.api import (
    SecretFormPrefill,
    SecretFormSession,
    normalize_placements,
    normalize_transform,
)
from octoforge_core.secrets.models import SecretFormLinkRow
from octoforge_core.time import utc_now

# 12 url-safe characters ≈ 72 bits: unguessable within a ten-minute life,
# still short enough to survive being retyped or read aloud
CODE_BYTES = 9


class SqlAlchemySecretFormLinkStore:
    """SQL persistence for short-lived secrets-form codes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def issue(
        self, user_id: str, prefill: SecretFormPrefill | None, ttl_seconds: float
    ) -> str:
        """Store a fresh code for that person and return it."""
        code = random_secrets.token_urlsafe(CODE_BYTES)
        now = utc_now()
        async with write_session(self._session_factory) as session:
            # opportunistic cleanup: dead codes have no reason to accumulate
            # and this table only ever sees a handful of rows at a time
            await session.execute(
                delete(SecretFormLinkRow).where(SecretFormLinkRow.expires_at < now)
            )
            session.add(
                SecretFormLinkRow(
                    code=code,
                    user_id=user_id,
                    prefill=_prefill_to_json(prefill),
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            )
        return code

    async def redeem(self, code: str) -> SecretFormSession | None:
        """Return what a live code opens; None when unknown or expired."""
        row = await self._row(code)
        if row is None or row.expires_at <= utc_now():
            return None
        return SecretFormSession(user_id=row.user_id, prefill=_prefill_from_json(row.prefill))

    async def is_expired(self, code: str) -> bool:
        """Whether this code existed and has run out — for an honest message."""
        row = await self._row(code)
        return row is not None and row.expires_at <= utc_now()

    async def _row(self, code: str) -> SecretFormLinkRow | None:
        async with read_session(self._session_factory) as session:
            result = await session.scalars(
                select(SecretFormLinkRow).where(SecretFormLinkRow.code == code)
            )
            return result.first()


def _prefill_to_json(prefill: SecretFormPrefill | None) -> str | None:
    if prefill is None:
        return None
    return json.dumps(
        {
            "code": prefill.code,
            "allowed_host": prefill.allowed_host,
            "description": prefill.description,
            "placements": sorted(member.value for member in prefill.placements),
            "transform": prefill.transform.value if prefill.transform is not None else None,
        }
    )


def _prefill_from_json(raw: str | None) -> SecretFormPrefill | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None  # a payload this version cannot read opens an empty form
    placements = data.get("placements")
    transform = data.get("transform")
    return SecretFormPrefill(
        code=str(data["code"]),
        allowed_host=str(data["allowed_host"]),
        description=str(data["description"]),
        placements=normalize_placements(
            [str(item) for item in placements] if isinstance(placements, list) else []
        ),
        transform=normalize_transform(str(transform) if transform is not None else None),
    )


__all__ = ["SqlAlchemySecretFormLinkStore"]
