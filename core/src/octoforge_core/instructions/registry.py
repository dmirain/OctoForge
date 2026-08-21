"""Declarative system registry and its startup reconciliation."""

from dataclasses import dataclass

from octoforge_core.instructions.api import (
    InstructionDefinition,
    InstructionService,
    InstructionType,
)
from octoforge_core.instructions.system_prompt_text import (
    ABOUT_OCTOFORGE_CONTENT,
    DEFERRED_WORK_CONTENT,
    EXTERNAL_HTTP_CONTENT,
    HISTORY_LOOKUP_CONTENT,
    SKILL_AUTHORING_CONTENT,
    USER_DATASETS_CONTENT,
    USER_MEMORY_CONTENT,
    WEB_LOOKUP_CONTENT,
)


@dataclass(frozen=True, slots=True)
class SystemSkill:
    """One installer-owned instruction kept present by the registry sync."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]


CORE_SYSTEM_SKILLS: tuple[SystemSkill, ...] = (
    SystemSkill(
        InstructionType.SKILL,
        "deferred_work",
        DEFERRED_WORK_CONTENT,
        ("cron", "scheduler", "tasks", "background", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "user_memory",
        USER_MEMORY_CONTENT,
        ("memory", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "user_datasets",
        USER_DATASETS_CONTENT,
        ("datasets", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "history_lookup",
        HISTORY_LOOKUP_CONTENT,
        ("history", "search", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "web_lookup",
        WEB_LOOKUP_CONTENT,
        ("web", "search", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "external_http",
        EXTERNAL_HTTP_CONTENT,
        ("http", "endpoint", "scenario"),
    ),
    SystemSkill(
        InstructionType.SKILL,
        "skill_authoring",
        SKILL_AUTHORING_CONTENT,
        ("skills", "authoring", "scenario"),
    ),
    SystemSkill(
        InstructionType.KNOWLEDGE,
        "about_octoforge",
        ABOUT_OCTOFORGE_CONTENT,
        ("identity", "system", "author", "capabilities"),
    ),
)


async def sync_system_registry(
    service: InstructionService,
    entries: tuple[SystemSkill, ...],
) -> None:
    """Replace only the registry-owned slice; user records remain untouched."""
    for entry in entries:
        await service.save_system(
            InstructionDefinition(entry.kind, entry.title, entry.content, entry.tags)
        )
    keep = {(entry.kind.value, entry.title) for entry in entries}
    for record in await service.list_system():
        if (record.type.value, record.title) not in keep:
            await service.delete_system(record.title, record.type)
