"""File-sourced overlay over the built-in system-skill registry.

The registry itself is code (`CORE_SYSTEM_SKILLS` + `WEB_SYSTEM_SKILLS`): it is
what every installation gets by default. This module is the installer's lever
on top of it — the same idea as `OF_SYSTEM_PROMPT_SOURCE` for prompts, applied
to scenarios: a JSON file (mounted into the container, never rebuilt) that
replaces, extends or adds records before the startup sync writes them.

Why an overlay rather than editing the registry: core stays language- and
deployment-neutral, while a concrete installation tunes what its users
actually say. Measured on this deployment (2026-07-26): the English core
scenarios sat at rank 9-15 for Russian phrasings of the same intent — a
`deferred_work` that nobody's "напомни завтра в 9" could ever reach. One
appended line of Russian trigger phrases moved it to rank 3-4 without
touching the English body or costing the English queries anything.

File format — a JSON list of entries, each keyed by (type, title):

    [
      {"type": "skill", "title": "deferred_work",
       "append": "Запросы по-русски: напомни, по расписанию, в фоне."},
      {"type": "knowledge", "title": "house_rules",
       "content": "...", "tags": ["local"]}
    ]

`append` extends the registry text of an existing record (the body stays in
code, the installation adds to it); `content` replaces it, and for a title the
registry does not know, creates a new system record. An entry may carry
`tags` to replace the record's tags. Anything unusable — a missing file, bad
JSON, an `append` for an unknown record — is a logged warning, never a failed
startup: a broken overlay must leave the built-in registry serving.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.registry import SystemSkill

logger = logging.getLogger(__name__)

FILE_SCHEME_PREFIX = "file:"
APPEND_SEPARATOR = "\n"


@dataclass(frozen=True, slots=True)
class SkillPatch:
    """One overlay entry: what to do with the (kind, title) record."""

    kind: InstructionType
    title: str
    content: str | None = None
    append: str | None = None
    tags: tuple[str, ...] | None = None


def parse_overlay_source(source: str) -> Path:
    """Resolve the configured overlay source into a path (`file:` scheme only)."""
    if not source.startswith(FILE_SCHEME_PREFIX):
        raise ValueError(
            f"unsupported system skills source {source!r}: only '{FILE_SCHEME_PREFIX}' is supported"
        )
    return Path(source.removeprefix(FILE_SCHEME_PREFIX))


def load_overlay(path: Path) -> tuple[SkillPatch, ...]:
    """Read the overlay file; an unusable file yields no patches (warning logged)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("system skills overlay unreadable, using the built-in registry: %s", path)
        return ()
    except json.JSONDecodeError:
        logger.warning("system skills overlay is not valid JSON, ignoring it: %s", path)
        return ()
    if not isinstance(raw, list):
        logger.warning("system skills overlay must be a JSON list, ignoring it: %s", path)
        return ()
    return tuple(patch for entry in raw if (patch := _parse_entry(entry)) is not None)


def apply_overlay(
    registry: tuple[SystemSkill, ...],
    patches: tuple[SkillPatch, ...],
) -> tuple[SystemSkill, ...]:
    """Return the registry with the patches applied, order preserved.

    Patches match by (kind, title): `append` extends the record's text,
    `content` replaces it. A patch for an unknown record appends a new entry
    when it carries `content`, and is dropped with a warning when it only
    knows how to `append` (there is nothing to append to — most likely a typo
    in the title, and silently creating a stub record would hide it).
    """
    by_key = {(patch.kind, patch.title): patch for patch in patches}
    patched = [_patched(entry, by_key.pop((entry.kind, entry.title), None)) for entry in registry]
    for (kind, title), patch in by_key.items():
        if patch.content is None:
            logger.warning(
                "system skills overlay patches an unknown record, skipped: %s %r",
                kind.value,
                title,
            )
            continue
        patched.append(
            SystemSkill(
                kind=kind,
                title=title,
                content=patch.content,
                tags=patch.tags if patch.tags is not None else (),
            )
        )
    return tuple(patched)


def _patched(entry: SystemSkill, patch: SkillPatch | None) -> SystemSkill:
    if patch is None:
        return entry
    content = entry.content
    if patch.content is not None:
        content = patch.content
    if patch.append is not None:
        content = f"{content}{APPEND_SEPARATOR}{patch.append}"
    return SystemSkill(
        kind=entry.kind,
        title=entry.title,
        content=content,
        tags=patch.tags if patch.tags is not None else entry.tags,
    )


def _parse_entry(entry: object) -> SkillPatch | None:
    """Validate one raw overlay entry; an unusable entry is dropped and logged."""
    if not isinstance(entry, dict):
        logger.warning("system skills overlay entry is not an object, skipped: %r", entry)
        return None
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        logger.warning("system skills overlay entry without a title, skipped: %r", entry)
        return None
    kind = _parse_kind(entry.get("type"), title)
    if kind is None:
        return None
    body = _parse_body(entry, title)
    if body is None:
        return None
    content, append = body
    return SkillPatch(
        kind=kind,
        title=title,
        content=content,
        append=append,
        tags=_parse_tags(entry.get("tags"), title),
    )


def _parse_kind(raw: object, title: str) -> InstructionType | None:
    try:
        kind = InstructionType(str(raw))
    except ValueError:
        logger.warning("system skills overlay entry with an unknown type, skipped: %r", title)
        return None
    if kind is InstructionType.MEMORY:
        # memories are per-user records; a registry-owned one cannot exist
        logger.warning("system skills overlay cannot declare a memory record, skipped: %r", title)
        return None
    return kind


def _parse_body(entry: dict[str, object], title: str) -> tuple[str | None, str | None] | None:
    """Return (content, append) of the entry, or None when it carries no usable body."""
    content = entry.get("content")
    append = entry.get("append")
    if content is None and append is None:
        logger.warning(
            "system skills overlay entry needs 'content' or 'append', skipped: %r", title
        )
        return None
    if (content is not None and not isinstance(content, str)) or (
        append is not None and not isinstance(append, str)
    ):
        logger.warning("system skills overlay entry has a non-string body, skipped: %r", title)
        return None
    return content, append


def _parse_tags(raw: object, title: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    logger.warning(
        "system skills overlay entry has non-string tags, keeping the originals: %r", title
    )
    return None
