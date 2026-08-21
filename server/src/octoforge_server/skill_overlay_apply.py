"""Apply validated skill patches while preserving registry order."""

import logging

from octoforge_core.instructions.registry import SystemSkill

from octoforge_server.skill_overlay_parse import SkillPatch

logger = logging.getLogger(__name__)
APPEND_SEPARATOR = "\n"


def apply_overlay(
    registry: tuple[SystemSkill, ...],
    patches: tuple[SkillPatch, ...],
) -> tuple[SystemSkill, ...]:
    by_key = {(patch.kind, patch.title): patch for patch in patches}
    patched = [_patched(entry, by_key.pop((entry.kind, entry.title), None)) for entry in registry]
    for (kind, title), patch in by_key.items():
        if patch.content is None:
            logger.warning("overlay patches unknown record, skipped: %s %r", kind.value, title)
            continue
        patched.append(
            SystemSkill(kind, title, patch.content, patch.tags if patch.tags is not None else ())
        )
    return tuple(patched)


def _patched(entry: SystemSkill, patch: SkillPatch | None) -> SystemSkill:
    if patch is None:
        return entry
    content = patch.content if patch.content is not None else entry.content
    if patch.append is not None:
        content = f"{content}{APPEND_SEPARATOR}{patch.append}"
    return SystemSkill(
        entry.kind,
        entry.title,
        content,
        patch.tags if patch.tags is not None else entry.tags,
    )
