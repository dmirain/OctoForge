"""Validation of raw system-skill overlay entries."""

import logging
from dataclasses import dataclass

from octoforge_core.instructions.api import InstructionType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillPatch:
    kind: InstructionType
    title: str
    content: str | None = None
    append: str | None = None
    tags: tuple[str, ...] | None = None


def parse_entry(entry: object) -> SkillPatch | None:
    if not isinstance(entry, dict):
        logger.warning("system skills overlay entry is not an object, skipped: %r", entry)
        return None
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        logger.warning("system skills overlay entry without a title, skipped: %r", entry)
        return None
    kind = _parse_kind(entry.get("type"), title)
    body = _parse_body(entry, title)
    if kind is None or body is None:
        return None
    content, append = body
    return SkillPatch(kind, title, content, append, _parse_tags(entry.get("tags"), title))


def _parse_kind(raw: object, title: str) -> InstructionType | None:
    try:
        kind = InstructionType(str(raw))
    except ValueError:
        logger.warning("system skills overlay entry with an unknown type, skipped: %r", title)
        return None
    if kind is InstructionType.MEMORY:
        logger.warning("system skills overlay cannot declare a memory record, skipped: %r", title)
        return None
    return kind


def _parse_body(
    entry: dict[str, object],
    title: str,
) -> tuple[str | None, str | None] | None:
    content = entry.get("content")
    append = entry.get("append")
    if content is None and append is None:
        logger.warning("overlay entry needs content or append, skipped: %r", title)
        return None
    if content is not None and not isinstance(content, str):
        logger.warning("overlay entry has a non-string body, skipped: %r", title)
        return None
    if append is not None and not isinstance(append, str):
        logger.warning("overlay entry has a non-string body, skipped: %r", title)
        return None
    return content, append


def _parse_tags(raw: object, title: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    logger.warning("overlay entry has non-string tags, keeping originals: %r", title)
    return None
