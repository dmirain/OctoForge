"""Tests for SkillRegistry."""

from typing import Any

import pytest

from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import DuplicateSkillError, SkillNotFoundError
from octoforge_core.skills.registry import SkillRegistry

SKILL_NAME = "dummy"
OTHER_SKILL_NAME = "other"
UNKNOWN_SKILL_NAME = "missing"
CTX = SkillContext(user_id="user-test", channel="web", dialog_id="dlg-test")


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

    registry.register(skill)

    assert registry.get(SKILL_NAME) is skill


def test_register_duplicate_name_rejected() -> None:
    registry = SkillRegistry()
    registry.register(DummySkill(SKILL_NAME))

    with pytest.raises(DuplicateSkillError):
        registry.register(DummySkill(SKILL_NAME))


def test_get_unknown_skill_raises() -> None:
    registry = SkillRegistry()

    with pytest.raises(SkillNotFoundError):
        registry.get(UNKNOWN_SKILL_NAME)


def test_specs_lists_all_registered() -> None:
    registry = SkillRegistry()
    registry.register(DummySkill(SKILL_NAME))
    registry.register(DummySkill(OTHER_SKILL_NAME))

    names = [spec.name for spec in registry.specs()]

    assert names == [SKILL_NAME, OTHER_SKILL_NAME]


class GatedSkill(DummySkill):
    """Skill with context-dependent visibility (duck-typed opt-in)."""

    def __init__(self, name: str, allowed_user_id: str) -> None:
        super().__init__(name)
        self._allowed_user_id = allowed_user_id

    def visible_to(self, context: SkillContext) -> bool:
        return context.user_id == self._allowed_user_id


def test_specs_without_context_lists_gated_skills() -> None:
    registry = SkillRegistry()
    registry.register(GatedSkill(SKILL_NAME, allowed_user_id="someone-else"))

    names = [spec.name for spec in registry.specs()]

    assert names == [SKILL_NAME]


def test_specs_with_context_honors_visible_to() -> None:
    registry = SkillRegistry()
    registry.register(DummySkill(OTHER_SKILL_NAME))
    registry.register(GatedSkill(SKILL_NAME, allowed_user_id=CTX.user_id))

    visible = [spec.name for spec in registry.specs(CTX)]
    stranger = SkillContext(user_id="user-stranger", channel=CTX.channel, dialog_id="dlg-x")
    hidden = [spec.name for spec in registry.specs(stranger)]

    assert visible == [OTHER_SKILL_NAME, SKILL_NAME]
    assert hidden == [OTHER_SKILL_NAME]
