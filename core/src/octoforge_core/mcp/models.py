"""ORM models of the MCP module; the tables are owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.mcp.api import DEFAULT_AUTH_FORMAT, DEFAULT_AUTH_HEADER
from octoforge_core.time import utc_now


class McpServerRow(Base):
    """One external MCP server, shared by the whole installation.

    Unique on `url` — the deduplication the table exists for: two people
    adding the same server must share one row. Unique on `name` too, because
    mirror records address the server by name.
    """

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(String, unique=True)
    # the per-user secret CODE and how to carry its value; never a value
    auth_secret_code: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    auth_header: Mapped[str] = mapped_column(String, default=DEFAULT_AUTH_HEADER)
    auth_format: Mapped[str] = mapped_column(String, default=DEFAULT_AUTH_FORMAT)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # hash of the last tool list the skill generator ruled on: the LLM is
    # consulted again only when the server's tools actually change
    tools_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class McpServerLinkRow(Base):
    """Who added a server. Lifecycle bookkeeping, not visibility:
    a synced server's tools are records everyone can recall."""

    __tablename__ = "mcp_server_links"

    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
