#!/usr/bin/env python
"""Copy an OctoForge SQLite database into Postgres, table by table."""

import argparse
import asyncio
import sys

from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.models import CronJobRow
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.db.base import Base
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.tasks.models import TaskRow
from octoforge_telegram.invites.models import InviteRow
from octoforge_telegram.schema import TelegramSurfaceBase
from sqlite_migration import Migration, migrate

CORE_TABLES: tuple[type[Base], ...] = (
    DialogRow,
    MessageRow,
    TaskRow,
    SummaryRow,
    CronJobRow,
    InstructionRow,
    DatasetRow,
    DatasetRecordRow,
)
INVITE_TABLES: tuple[type[TelegramSurfaceBase], ...] = (InviteRow,)
EXIT_MISMATCH = 1


async def main() -> int:
    args = _parser().parse_args()
    print("application database")
    matched = await migrate(
        Migration(args.source, args.target, CORE_TABLES, True, args.force)
    )
    if args.invite_source and args.invite_target:
        print("invite database")
        invites = Migration(
            args.invite_source,
            args.invite_target,
            INVITE_TABLES,
            False,
            args.force,
            TelegramSurfaceBase,
        )
        matched = await migrate(invites) and matched
    elif args.invite_source or args.invite_target:
        print("both --invite-source and --invite-target are required to copy invites")
        return EXIT_MISMATCH
    print("done" if matched else "FAILED: row counts differ")
    return 0 if matched else EXIT_MISMATCH


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--invite-source")
    parser.add_argument("--invite-target")
    parser.add_argument("--force", action="store_true")
    return parser


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
