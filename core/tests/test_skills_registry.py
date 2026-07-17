"""Tests for SkillRegistry."""

from typing import Any

import pytest

from octoforge_core.skills.base import SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.errors import DuplicateSkillError, SkillNotFoundError
from octoforge_core.skills.registry import SkillRegistry

SKILL_NAME = "dummy"
OTHER_SKILL_NAME = "other"
UNKNOWN_SKILL_NAME = "missing"
CTX = SkillContext(conversation_id="conv-test")


class DummySkill:
    """Minimal skill stub."""

    def __init__(self, name: str) -> None:
        self._spec = SkillSpec(name=name, description="stub", parameters_schema={})

    @property
    def spec(self) -> SkillSpec:
        return self._spec

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        return "ok"


def test_register_and_get() -> None:
    registry = SkillRegistry()
    skill = DummySkill(SKILL_NAME)

    registry.register(skill, SkillOrigin.BASIC)

    assert registry.get(SKILL_NAME) is skill
    assert registry.origin_of(SKILL_NAME) is SkillOrigin.BASIC


def test_register_duplicate_name_rejected() -> None:
    registry = SkillRegistry()
    registry.register(DummySkill(SKILL_NAME), SkillOrigin.BASIC)

    with pytest.raises(DuplicateSkillError):
        registry.register(DummySkill(SKILL_NAME), SkillOrigin.DYNAMIC)


def test_get_unknown_skill_raises() -> None:
    registry = SkillRegistry()

    with pytest.raises(SkillNotFoundError):
        registry.get(UNKNOWN_SKILL_NAME)


def test_origin_of_unknown_skill_raises() -> None:
    registry = SkillRegistry()

    with pytest.raises(SkillNotFoundError):
        registry.origin_of(UNKNOWN_SKILL_NAME)


def test_specs_lists_all_registered() -> None:
    registry = SkillRegistry()
    registry.register(DummySkill(SKILL_NAME), SkillOrigin.BASIC)
    registry.register(DummySkill(OTHER_SKILL_NAME), SkillOrigin.BASIC)

    names = [spec.name for spec in registry.specs()]

    assert names == [SKILL_NAME, OTHER_SKILL_NAME]
