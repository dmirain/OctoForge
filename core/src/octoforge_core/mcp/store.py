"""SQL store of the MCP module."""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.mcp.api import McpServer, McpServerList, McpServerNameTakenError
from octoforge_core.mcp.models import McpServerLinkRow, McpServerRow
from octoforge_core.time import utc_now


class SqlAlchemyMcpServerStore:
    """Shared MCP server rows and the links naming who added them."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, server: McpServer, user_id: str) -> McpServer:
        """Register the server for this user, deduplicating by URL.

        Deliberately NOT unit-of-work aware (raw `_session_factory`): the
        insert branch uses the commit itself as the uniqueness-race detector —
        two first adds arriving together must converge on one row, so the
        loser rolls back and links the winner's row instead.
        """
        async with self._session_factory() as session:
            existing = await self._find_by_url(session, server.url)
            if existing is None:
                row = McpServerRow(
                    id=server.id,
                    name=server.name,
                    url=server.url,
                    auth_secret_code=server.auth_secret_code,
                    auth_header=server.auth_header,
                    auth_format=server.auth_format,
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await self._find_by_url(session, server.url)
                    if existing is None:
                        # the clash was on `name`, not `url`: same name, other URL
                        raise McpServerNameTakenError(server.name) from None
                else:
                    existing = row
            await self._link(session, existing.id, user_id)
            return _to_server(existing)

    async def get_by_name(self, name: str) -> McpServer | None:
        async with read_session(self._session_factory) as session:
            row = (
                await session.scalars(select(McpServerRow).where(McpServerRow.name == name))
            ).first()
            return _to_server(row) if row is not None else None

    async def list_all(self) -> McpServerList:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(McpServerRow).order_by(McpServerRow.created_at, McpServerRow.id)
            )
            return [_to_server(row) for row in rows.all()]

    async def list_user_ids(self, server_id: str) -> list[str]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(McpServerLinkRow.user_id)
                .where(McpServerLinkRow.server_id == server_id)
                .order_by(McpServerLinkRow.created_at, McpServerLinkRow.user_id)
            )
            return list(rows.all())

    async def mark_synced(self, server_id: str, error: str | None) -> None:
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(McpServerRow)
                .where(McpServerRow.id == server_id)
                .values(last_synced_at=utc_now(), last_sync_error=error, updated_at=utc_now())
            )

    async def set_tools_fingerprint(self, server_id: str, fingerprint: str) -> None:
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(McpServerRow)
                .where(McpServerRow.id == server_id)
                .values(tools_fingerprint=fingerprint, updated_at=utc_now())
            )

    @staticmethod
    async def _find_by_url(session: AsyncSession, url: str) -> McpServerRow | None:
        return (await session.scalars(select(McpServerRow).where(McpServerRow.url == url))).first()

    @staticmethod
    async def _link(session: AsyncSession, server_id: str, user_id: str) -> None:
        """Insert the link, tolerating that it (or a race for it) already exists."""
        found = await session.get(McpServerLinkRow, (server_id, user_id))
        if found is not None:
            return
        session.add(McpServerLinkRow(server_id=server_id, user_id=user_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # linked concurrently: the state we wanted


def _to_server(row: McpServerRow) -> McpServer:
    return McpServer(
        id=row.id,
        name=row.name,
        url=row.url,
        auth_secret_code=row.auth_secret_code,
        auth_header=row.auth_header,
        auth_format=row.auth_format,
        last_synced_at=row.last_synced_at,
        last_sync_error=row.last_sync_error,
        tools_fingerprint=row.tools_fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
