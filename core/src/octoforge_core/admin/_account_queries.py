"""Admin listings for user parameters and secret metadata."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin._account_rows import to_secret
from octoforge_core.admin._page import run_page
from octoforge_core.admin.account_types import SecretOverview, UserParamOverview
from octoforge_core.admin.requests import PageRequest
from octoforge_core.admin.types import Page
from octoforge_core.params.models import UserParamRow
from octoforge_core.secrets.models import SecretRow


async def list_user_params(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[UserParamOverview]:
    statement = (
        select(UserParamRow)
        .order_by(UserParamRow.user_id, UserParamRow.code)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(UserParamRow),
    )
    items = tuple(
        UserParamOverview(row.user_id, row.code, row.value, row.updated_at) for row in rows
    )
    return Page(items, total, page.limit, page.offset)


async def list_secrets(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[SecretOverview]:
    statement = (
        select(SecretRow)
        .order_by(SecretRow.user_id, SecretRow.code)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(SecretRow),
    )
    return Page(tuple(to_secret(row) for row in rows), total, page.limit, page.offset)
