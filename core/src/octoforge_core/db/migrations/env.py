"""Alembic environment for OctoForge's schema.

Two entry paths share one migration routine:
- CLI (autogenerate / manual upgrade): a sync engine is built from the
  configured ``sqlalchemy.url``.
- Programmatic startup: the composition root passes a live (sync) connection
  through ``config.attributes["connection"]`` (obtained via ``run_sync`` on the
  async engine), so no second engine is opened.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from octoforge_core.db.base import Base

# Import every model module so all tables register on Base.metadata.
import octoforge_core.cron.models  # noqa: E402,F401
import octoforge_core.datasets.models  # noqa: E402,F401
import octoforge_core.db.models  # noqa: E402,F401
import octoforge_core.instructions.models  # noqa: E402,F401
import octoforge_core.memory.models  # noqa: E402,F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migrations as SQL without a DBAPI connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTERs
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection (provided or freshly built)."""
    connection = config.attributes.get("connection", None)
    if connection is not None:
        do_run_migrations(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
