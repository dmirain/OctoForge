#!/usr/bin/env python
"""Copy an OctoForge SQLite database into Postgres, table by table.

A one-off migration tool, not part of either package: it imports the ORM models
of both schemas so `UTCDateTime` handles the timezone difference on each side
(naive UTC in SQLite, timestamptz in Postgres) instead of moving raw values.

Tables are copied in foreign-key order and verified by row count; the invite
database (its own Base, no Alembic) is copied separately when both URLs are
given. The target is created with `bootstrap_schema`, which on an empty
non-SQLite database means create_all plus a stamp at head.

    python tools/sqlite_to_postgres.py \
        --source sqlite+aiosqlite:///./octoforge.db \
        --target postgresql+asyncpg://octoforge:octoforge@127.0.0.1:5432/octoforge \
        --invite-source sqlite+aiosqlite:///./telegram.db \
        --invite-target postgresql+asyncpg://octoforge:octoforge@127.0.0.1:5432/octoforge_telegram

Stop every writer first: a bot or web instance running against the source can
append rows between the copy and the count check.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.models import CronJobRow
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.db.base import Base
from octoforge_core.db.engine import (
    bootstrap_schema,
    create_engine,
    create_session_factory,
)
from octoforge_core.db.models import DialogRow, MessageRow, TaskRow
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.memory.models import MemoryRow
from octoforge_web.telegram.invites.models import InviteBase, InviteRow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# Parents before children: messages/tasks/dialog_summaries reference dialogs,
# dataset_records references datasets.
CORE_TABLES: tuple[type[Base], ...] = (
    DialogRow,
    MessageRow,
    TaskRow,
    SummaryRow,
    CronJobRow,
    InstructionRow,
    DatasetRow,
    DatasetRecordRow,
    MemoryRow,
)
INVITE_TABLES: tuple[type[InviteBase], ...] = (InviteRow,)
BATCH_SIZE = 500
EXIT_MISMATCH = 1


async def copy_table(
    model: type[DeclarativeBase],
    source_factory: async_sessionmaker[AsyncSession],
    target_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    """Copy every row of one table; return (source count, target count).

    Rows are re-created as fresh ORM instances from the loaded column values,
    so each side's `TypeDecorator` runs: SQLite hands over naive UTC, Postgres
    stores an aware value.
    """
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


async def migrate(
    source_url: str,
    target_url: str,
    models: Sequence[type[DeclarativeBase]],
    *,
    prepare_target: bool,
    force: bool,
) -> bool:
    """Copy one database; return whether every table matched."""
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)
    try:
        if prepare_target:
            await bootstrap_schema(target_engine)
        else:
            await _create_invite_schema(target_engine)
        source_factory = create_session_factory(source_engine)
        target_factory = create_session_factory(target_engine)
        if not force and not await target_is_empty(models, target_factory):
            print(
                f"target {target_url.rsplit('/', 1)[-1]} already holds rows; pass --force"
            )
            return False
        matched = True
        for model in models:
            read, written = await copy_table(model, source_factory, target_factory)
            status = "ok" if read == written else "MISMATCH"
            print(
                f"  {model.__tablename__:18} source={read:5} target={written:5}  {status}"
            )
            matched = matched and read == written
        return matched
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


async def _create_invite_schema(engine: AsyncEngine) -> None:
    """The invite schema has no Alembic chain of its own (see the web adapter)."""
    async with engine.begin() as connection:
        await connection.run_sync(InviteBase.metadata.create_all)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, help="SQLite URL of the application database"
    )
    parser.add_argument("--target", required=True, help="Postgres URL to copy it into")
    parser.add_argument(
        "--invite-source", help="SQLite URL of the Telegram invite database"
    )
    parser.add_argument("--invite-target", help="Postgres URL for the invite database")
    parser.add_argument(
        "--force",
        action="store_true",
        help="copy even when the target already holds rows",
    )
    args = parser.parse_args()

    print("application database")
    matched = await migrate(
        args.source, args.target, CORE_TABLES, prepare_target=True, force=args.force
    )

    if args.invite_source and args.invite_target:
        print("invite database")
        matched = (
            await migrate(
                args.invite_source,
                args.invite_target,
                INVITE_TABLES,
                prepare_target=False,
                force=args.force,
            )
            and matched
        )
    elif args.invite_source or args.invite_target:
        print("both --invite-source and --invite-target are required to copy invites")
        return EXIT_MISMATCH

    print("done" if matched else "FAILED: row counts differ")
    return 0 if matched else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
