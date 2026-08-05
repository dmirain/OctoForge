"""SQLAlchemy implementations of the TariffStore and UsageMeter ports."""

import uuid
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tariffs.api import (
    Tariff,
    TariffInUseError,
    TariffLimits,
    TariffNotFoundError,
    UsageEvent,
    UsageKind,
    UsageTotals,
    normalize_code,
    normalize_feature,
    normalize_title,
)
from octoforge_core.tariffs.models import TariffRow, UsageEventRow, UserTariffRow
from octoforge_core.time import utc_now


class SqlAlchemyTariffStore:
    """SQL persistence for the tariff catalog and user assignments.

    Writes come from the operator console only — one writer per row — so the
    read-then-update upsert is race-free enough here, unlike the usage ledger.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(
        self,
        code: str,
        title: str,
        features: frozenset[str],
        limits: TariffLimits | None = None,
    ) -> Tariff:
        """Create or replace the tariff under `code`.

        Feature codes are grammar-checked only: the store must accept an
        installer-defined code it has never heard of — the known-vocabulary
        check belongs to the operator boundary, where the merged set lives.
        """
        code = normalize_code(code)
        title = normalize_title(title)
        caps = (limits or TariffLimits()).normalized()
        async with write_session(self._session_factory) as session:
            row = await _find_tariff(session, code)
            if row is None:
                row = TariffRow(id=uuid.uuid4().hex, code=code, title=title)
                session.add(row)
            else:
                row.title = title
                row.updated_at = utc_now()
            row.features = sorted({normalize_feature(feature) for feature in features})
            for name, value in asdict(caps).items():
                setattr(row, name, value)
            await session.flush()
            return _to_tariff(row)

    async def list(self) -> list[Tariff]:
        """Return every tariff ordered by code."""
        async with read_session(self._session_factory) as session:
            rows = (await session.scalars(select(TariffRow).order_by(TariffRow.code))).all()
            return [_to_tariff(row) for row in rows]

    async def delete(self, code: str) -> None:
        """Delete a tariff; raise `TariffInUseError` while users are assigned."""
        code = normalize_code(code)
        async with write_session(self._session_factory) as session:
            row = await _find_tariff(session, code)
            if row is None:
                raise TariffNotFoundError(code)
            assigned = await session.scalar(
                select(func.count())
                .select_from(UserTariffRow)
                .where(UserTariffRow.tariff_id == row.id)
            )
            if assigned:
                raise TariffInUseError(f"tariff '{code}' is assigned to {assigned} user(s)")
            await session.delete(row)

    async def assign(self, user_id: str, code: str | None) -> None:
        """Bind the user to the tariff, or unbind with `None` (= unlimited)."""
        async with write_session(self._session_factory) as session:
            binding = (
                await session.scalars(select(UserTariffRow).where(UserTariffRow.user_id == user_id))
            ).first()
            if code is None:
                if binding is not None:
                    await session.delete(binding)
                return
            tariff = await _find_tariff(session, normalize_code(code))
            if tariff is None:
                raise TariffNotFoundError(code)
            if binding is None:
                session.add(
                    UserTariffRow(id=uuid.uuid4().hex, user_id=user_id, tariff_id=tariff.id)
                )
            else:
                binding.tariff_id = tariff.id
                binding.assigned_at = utc_now()

    async def tariff_for_user(self, user_id: str) -> Tariff | None:
        """Return the user's tariff; `None` means no restrictions."""
        async with read_session(self._session_factory) as session:
            row = (
                await session.scalars(
                    select(TariffRow)
                    .join(UserTariffRow, UserTariffRow.tariff_id == TariffRow.id)
                    .where(UserTariffRow.user_id == user_id)
                )
            ).first()
            return None if row is None else _to_tariff(row)

    async def assignments(self) -> dict[str, str]:
        """Return a user_id → tariff code mapping of every assignment."""
        async with read_session(self._session_factory) as session:
            pairs = (
                await session.execute(
                    select(UserTariffRow.user_id, TariffRow.code).join(
                        TariffRow, TariffRow.id == UserTariffRow.tariff_id
                    )
                )
            ).all()
            return {user_id: code for user_id, code in pairs}


class SqlAlchemyUsageMeter:
    """Insert-only usage ledger; safe under concurrent writers on both nodes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: UsageEvent) -> None:
        """Append one usage event; `created_at` is stamped here."""
        async with write_session(self._session_factory) as session:
            session.add(
                UsageEventRow(
                    id=uuid.uuid4().hex,
                    user_id=event.user_id,
                    kind=event.kind.value,
                    origin=event.origin.value,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    quantity=event.quantity,
                    dialog_id=event.dialog_id,
                    exchange_id=event.exchange_id,
                    task_id=event.task_id,
                    created_at=event.created_at or utc_now(),
                )
            )

    async def totals_since(self, user_id: str, since: datetime) -> UsageTotals:
        """Aggregate the user's events at or after `since` in one indexed scan."""

        def counted(kind: UsageKind) -> ColumnElement[int]:
            return func.sum(case((UsageEventRow.kind == kind.value, UsageEventRow.quantity)))

        async with read_session(self._session_factory) as session:
            row = (
                await session.execute(
                    select(
                        func.sum(UsageEventRow.prompt_tokens),
                        func.sum(UsageEventRow.completion_tokens),
                        counted(UsageKind.USER_MESSAGE),
                        counted(UsageKind.LLM_ANSWER),
                    ).where(
                        UsageEventRow.user_id == user_id,
                        UsageEventRow.created_at >= since,
                    )
                )
            ).one()
            prompt, completion, user_messages, answers = row
            return UsageTotals(
                prompt_tokens=prompt or 0,
                completion_tokens=completion or 0,
                user_messages=user_messages or 0,
                assistant_messages=answers or 0,
            )


async def _find_tariff(session: AsyncSession, code: str) -> TariffRow | None:
    result = await session.scalars(select(TariffRow).where(TariffRow.code == code))
    return result.first()


def _to_tariff(row: TariffRow) -> Tariff:
    return Tariff(
        id=row.id,
        code=row.code,
        title=row.title,
        features=frozenset(row.features),
        limits=TariffLimits(
            daily_tokens=row.daily_tokens,
            daily_user_messages=row.daily_user_messages,
            daily_assistant_messages=row.daily_assistant_messages,
            max_cron_jobs=row.max_cron_jobs,
            max_datasets=row.max_datasets,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
