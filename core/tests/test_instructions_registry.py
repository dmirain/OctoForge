"""Tests for the declarative system registry and its startup sync."""

from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.registry import (
    CORE_SYSTEM_SKILLS,
    SystemSkill,
    sync_system_registry,
)
from octoforge_core.instructions.store import SqlAlchemyInstructionStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
FIRST_VERSION = 1
SECOND_VERSION = 2
CORE_SKILLS_COUNT = 8

ENTRY_ALPHA = SystemSkill(
    kind=InstructionType.SKILL,
    title="alpha scenario",
    content="do A",
    tags=("core",),
)
ENTRY_BETA = SystemSkill(
    kind=InstructionType.ENDPOINT,
    title="beta endpoint",
    content='{"method": "GET", "url_template": "https://example.com/{x}"}',
    tags=("core",),
)


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


@pytest.fixture
async def service() -> AsyncIterator[InstructionService]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield LocalInstructionService(
        SqlAlchemyInstructionStore(create_session_factory(engine)), LenientEmbedder()
    )
    await engine.dispose()


async def test_core_registry_covers_the_eight_module_scenarios() -> None:
    assert len(CORE_SYSTEM_SKILLS) == CORE_SKILLS_COUNT
    assert {entry.kind for entry in CORE_SYSTEM_SKILLS} == {InstructionType.SKILL}
    titles = {entry.title for entry in CORE_SYSTEM_SKILLS}
    assert titles == {
        "cron_jobs",
        "background_tasks",
        "user_memory",
        "user_datasets",
        "history_lookup",
        "web_lookup",
        "external_http",
        "skill_authoring",
    }


async def test_sync_creates_system_records(service: InstructionService) -> None:
    await sync_system_registry(service, (ENTRY_ALPHA, ENTRY_BETA))

    stored = await service.get_by_name(ENTRY_ALPHA.title, InstructionType.SKILL)
    assert stored.content == ENTRY_ALPHA.content
    assert stored.tags == ENTRY_ALPHA.tags
    assert stored.system is True
    assert {record.title for record in await service.list_system()} == {
        ENTRY_ALPHA.title,
        ENTRY_BETA.title,
    }


async def test_sync_adopts_a_legacy_user_record(service: InstructionService) -> None:
    legacy = await service.save(
        InstructionType.SKILL, ENTRY_ALPHA.title, "legacy scenario", ("legacy",)
    )

    await sync_system_registry(service, (ENTRY_ALPHA,))

    adopted = await service.get_by_name(ENTRY_ALPHA.title, InstructionType.SKILL)
    assert adopted.id == legacy.id
    assert adopted.system is True
    assert adopted.content == ENTRY_ALPHA.content
    assert adopted.version == SECOND_VERSION


async def test_sync_deletes_system_records_missing_from_the_registry(
    service: InstructionService,
) -> None:
    await sync_system_registry(service, (ENTRY_ALPHA, ENTRY_BETA))

    await sync_system_registry(service, (ENTRY_ALPHA,))

    assert [record.title for record in await service.list_system()] == [ENTRY_ALPHA.title]
    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(ENTRY_BETA.title, InstructionType.ENDPOINT)


async def test_sync_never_touches_user_records(service: InstructionService) -> None:
    await service.save(InstructionType.SKILL, "my scenario", "user content", ("mine",))

    await sync_system_registry(service, (ENTRY_ALPHA,))

    user_record = await service.get_by_name("my scenario", InstructionType.SKILL)
    assert user_record.system is False
    assert user_record.content == "user content"
    assert user_record.version == FIRST_VERSION
    # the second sync must not delete it either (only system records are pruned)
    await sync_system_registry(service, (ENTRY_BETA,))
    assert (await service.get_by_name("my scenario", InstructionType.SKILL)).system is False
