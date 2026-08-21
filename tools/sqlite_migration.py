"""Table-by-table database copy with row-count verification."""

from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.composition_schema import bootstrap_schema
from octoforge_core.db.engine import create_engine, create_session_factory
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class Migration:
    source_url: str
    target_url: str
    models: Sequence[type[DeclarativeBase]]
    prepare_target: bool
    force: bool
    target_base: type[DeclarativeBase] | None = None


async def migrate(request: Migration) -> bool:
    source_engine = create_engine(request.source_url)
    target_engine = create_engine(request.target_url)
    try:
        if request.prepare_target:
            await bootstrap_schema(target_engine)
        elif request.target_base is not None:
            await _create_schema(target_engine, request.target_base)
        source_factory = create_session_factory(source_engine)
        target_factory = create_session_factory(target_engine)
        if not request.force and not await target_is_empty(
            request.models, target_factory
        ):
            print(
                f"target {request.target_url.rsplit('/', 1)[-1]} holds rows; pass --force"
            )
            return False
        results = [
            await copy_table(model, source_factory, target_factory)
            for model in request.models
        ]
        return all(read == written for read, written in results)
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


async def copy_table(
    model: type[DeclarativeBase],
    source_factory: async_sessionmaker[AsyncSession],
    target_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    table = model.__table__
    columns = [column.name for column in table.columns]
    async with source_factory() as source:
        rows = (await source.execute(select(model))).scalars().all()
        payloads = [{name: getattr(row, name) for name in columns} for row in rows]
    for start in range(0, len(payloads), BATCH_SIZE):
        async with target_factory() as target:
            target.add_all(
                [model(**payload) for payload in payloads[start : start + BATCH_SIZE]]
            )
            await target.commit()
    async with target_factory() as target:
        copied = (
            await target.execute(select(func.count()).select_from(table))
        ).scalar_one()
    print(f"  {model.__tablename__:18} source={len(payloads):5} target={int(copied):5}")
    return len(payloads), int(copied)


async def target_is_empty(
    models: Sequence[type[DeclarativeBase]],
    factory: async_sessionmaker[AsyncSession],
) -> bool:
    async with factory() as session:
        for model in models:
            count = (
                await session.execute(select(func.count()).select_from(model.__table__))
            ).scalar_one()
            if count:
                return False
    return True


async def _create_schema(engine: AsyncEngine, base: type[DeclarativeBase]) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(base.metadata.create_all)
