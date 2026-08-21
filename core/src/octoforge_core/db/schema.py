"""Create, adopt and migrate the relational schema."""

from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from octoforge_core.db.base import Base
from octoforge_core.db.engine_runtime import SQLITE_DIALECT
from octoforge_core.db.model_registry import register_models
from octoforge_core.db.search_extensions import ensure_bm25_indexes, ensure_search_extensions

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "675056c8fffd"
SchemaSeeder = Callable[[Connection], None]


async def init_db(engine: AsyncEngine) -> None:
    """Create current metadata directly for tests and fallback startup."""
    register_models()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def bootstrap_schema(engine: AsyncEngine, seeder: SchemaSeeder | None = None) -> None:
    """Bring a fresh, legacy or managed database to Alembic head."""
    register_models()
    async with engine.connect() as connection:
        await connection.run_sync(bootstrap_sync, seeder)
        await connection.commit()


def bootstrap_sync(connection: Connection, seeder: SchemaSeeder | None = None) -> None:
    tables = set(inspect(connection).get_table_names())
    config = alembic_config(connection)
    if not tables and connection.dialect.name != SQLITE_DIALECT:
        _create_and_stamp(connection, config, seeder)
        return
    if tables and "alembic_version" not in tables:
        adopted = BASELINE_REVISION if "memories" in tables else "head"
        command.stamp(config, adopted)
    command.upgrade(config, "head")


def alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def _create_and_stamp(
    connection: Connection,
    config: Config,
    seeder: SchemaSeeder | None,
) -> None:
    ensure_search_extensions(connection)
    Base.metadata.create_all(connection)
    ensure_bm25_indexes(connection)
    if seeder is not None:
        seeder(connection)
    command.stamp(config, "head")
