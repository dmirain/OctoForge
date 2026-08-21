"""File-sourced overlay over the built-in system-skill registry."""

import json
import logging
from pathlib import Path

from octoforge_server.skill_overlay_apply import apply_overlay
from octoforge_server.skill_overlay_parse import SkillPatch, parse_entry

logger = logging.getLogger(__name__)
FILE_SCHEME_PREFIX = "file:"

__all__ = ["SkillPatch", "apply_overlay", "load_overlay", "parse_overlay_source"]


def parse_overlay_source(source: str) -> Path:
    if not source.startswith(FILE_SCHEME_PREFIX):
        raise ValueError(f"unsupported system skills source {source!r}: only 'file:' is supported")
    return Path(source.removeprefix(FILE_SCHEME_PREFIX))


def load_overlay(path: Path) -> tuple[SkillPatch, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("system skills overlay unreadable, using built-ins: %s", path)
        return ()
    except json.JSONDecodeError:
        logger.warning("system skills overlay is invalid JSON, ignoring: %s", path)
        return ()
    if not isinstance(raw, list):
        logger.warning("system skills overlay must be a JSON list, ignoring: %s", path)
        return ()
    return tuple(patch for entry in raw if (patch := parse_entry(entry)) is not None)
