"""SQL lookup shared by secret commands and resolution."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_core.secrets.models import SecretRow


async def find_secret_row(session: AsyncSession, user_id: str, code: str) -> SecretRow | None:
    result = await session.scalars(
        select(SecretRow).where(SecretRow.user_id == user_id, SecretRow.code == code)
    )
    return result.first()
