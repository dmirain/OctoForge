"""Cross-domain relational schema bootstrap."""

from sqlalchemy.ext.asyncio import AsyncEngine

from octoforge_core.db.schema import bootstrap_schema as bootstrap_relational_schema
from octoforge_core.tariffs.seed import seed_starter_tariffs


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """Bring the schema to head and seed fresh-install domain records."""
    await bootstrap_relational_schema(engine, seed_starter_tariffs)
