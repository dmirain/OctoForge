"""Declarative base of the Telegram surface's own schema.

Separate from core's `Base` on purpose: these tables belong to a transport
adapter, and keeping them out of `Base.metadata` means they cannot leak into
core's Alembic chain or its tooling even by accident. There is no migration
chain here — a couple of small isolated tables, created with `create_all`.
"""

from sqlalchemy.orm import DeclarativeBase


class TelegramSurfaceBase(DeclarativeBase):
    """Base class of every table the Telegram surface owns."""
