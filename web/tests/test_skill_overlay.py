"""Tests for the file-sourced system-skill overlay (installer's registry lever)."""

import json
import logging
from pathlib import Path

import pytest
from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.registry import CORE_SYSTEM_SKILLS, SystemSkill

from octoforge_web.config import Settings
from octoforge_web.skill_overlay import (
    apply_overlay,
    load_overlay,
    parse_overlay_source,
)
from octoforge_web.system_skills import WEB_SYSTEM_SKILLS

BASE = (
    SystemSkill(
        kind=InstructionType.SKILL,
        title="deferred_work",
        content="Scenario: deferred work.",
        tags=("cron", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.KNOWLEDGE,
        title="about_octoforge",
        content="About this system.",
        tags=("identity",),
    ),
)
TRIGGERS = "Запросы по-русски: напомни, по расписанию."
LOGGER = "octoforge_web.skill_overlay"
TWO_ENTRIES = 2
THREE_ENTRIES = 3


def write_overlay(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "system_skills.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_append_extends_the_registry_text_and_keeps_tags(tmp_path: Path) -> None:
    """The installation adds trigger phrases; the scenario body stays in code."""
    path = write_overlay(
        tmp_path, [{"type": "skill", "title": "deferred_work", "append": TRIGGERS}]
    )

    patched = apply_overlay(BASE, load_overlay(path))

    assert patched[0].content == f"Scenario: deferred work.\n{TRIGGERS}"
    assert patched[0].tags == ("cron", "scenario")
    assert patched[1] == BASE[1]  # untouched records pass through unchanged


def test_content_replaces_and_tags_override(tmp_path: Path) -> None:
    path = write_overlay(
        tmp_path,
        [
            {
                "type": "knowledge",
                "title": "about_octoforge",
                "content": "Локальная инсталляция.",
                "tags": ["identity", "ru"],
            }
        ],
    )

    patched = apply_overlay(BASE, load_overlay(path))

    assert patched[1].content == "Локальная инсталляция."
    assert patched[1].tags == ("identity", "ru")


def test_unknown_title_with_content_adds_a_record(tmp_path: Path) -> None:
    """A deployment can add whole scenarios without touching the code."""
    path = write_overlay(
        tmp_path,
        [{"type": "skill", "title": "house_rules", "content": "Правила дома.", "tags": ["local"]}],
    )

    patched = apply_overlay(BASE, load_overlay(path))

    assert len(patched) == THREE_ENTRIES
    assert patched[-1].title == "house_rules"
    assert patched[-1].kind is InstructionType.SKILL


def test_append_to_an_unknown_title_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Most likely a typo: creating a stub record would hide it."""
    path = write_overlay(tmp_path, [{"type": "skill", "title": "typo_here", "append": TRIGGERS}])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        patched = apply_overlay(BASE, load_overlay(path))

    assert patched == BASE
    assert any("unknown record" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "skill", "title": "deferred_work"},  # no body
        {"type": "nonsense", "title": "deferred_work", "append": TRIGGERS},
        {"type": "memory", "title": "user_note", "content": "x"},  # never registry-owned
        {"type": "skill", "append": TRIGGERS},  # no title
        {"type": "skill", "title": "deferred_work", "append": 42},
        "not an object",
    ],
)
def test_unusable_entries_are_dropped(tmp_path: Path, entry: object) -> None:
    path = write_overlay(tmp_path, [entry])  # type: ignore[list-item]

    assert apply_overlay(BASE, load_overlay(path)) == BASE


def test_broken_files_leave_the_registry_serving(tmp_path: Path) -> None:
    """A bad overlay must never take the startup down."""
    missing = tmp_path / "absent.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    not_a_list = tmp_path / "object.json"
    not_a_list.write_text('{"type": "skill"}', encoding="utf-8")

    for path in (missing, invalid, not_a_list):
        assert load_overlay(path) == ()
        assert apply_overlay(BASE, load_overlay(path)) == BASE


def test_non_string_tags_keep_the_originals(tmp_path: Path) -> None:
    path = write_overlay(
        tmp_path,
        [{"type": "skill", "title": "deferred_work", "append": TRIGGERS, "tags": [1, 2]}],
    )

    patched = apply_overlay(BASE, load_overlay(path))

    assert patched[0].tags == ("cron", "scenario")


def test_settings_resolve_the_source(tmp_path: Path) -> None:
    path = tmp_path / "skills.json"

    resolved = Settings(system_skills_source=f"file:{path}").to_skills_overlay_path()

    assert resolved == path
    assert Settings().to_skills_overlay_path() is None
    with pytest.raises(ValueError, match="only 'file:'"):
        parse_overlay_source("https://example.com/skills.json")


def test_shipped_russian_overlay_matches_the_registry() -> None:
    """The deployment's overlay must not silently patch a renamed scenario."""
    overlay = Path(__file__).resolve().parents[2] / "docker" / "system_skills.ru.json"
    patches = load_overlay(overlay)
    known = {(entry.kind, entry.title) for entry in CORE_SYSTEM_SKILLS + WEB_SYSTEM_SKILLS}

    assert patches  # the file parses and carries entries
    unknown = [patch.title for patch in patches if (patch.kind, patch.title) not in known]
    assert unknown == []
