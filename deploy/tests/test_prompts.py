"""Tests for the file-backed PromptProvider (web-side prompt sources)."""

from pathlib import Path

import pytest
from octoforge_core.agent.prompts import (
    ROUTER_PROMPT_NAME,
    SYSTEM_PROMPT_NAME,
    StaticPromptProvider,
)
from octoforge_server.prompts import FilePromptProvider

CUSTOM_SYSTEM_PROMPT = "You are a custom system prompt.\n"
UNKNOWN_NAME = "no-such-prompt"


def write_prompt(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_prompt_is_read_from_the_file(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, "system.txt", CUSTOM_SYSTEM_PROMPT)
    provider = FilePromptProvider(
        files={SYSTEM_PROMPT_NAME: path},
        fallback=StaticPromptProvider(),
    )

    assert provider.get(SYSTEM_PROMPT_NAME) == CUSTOM_SYSTEM_PROMPT


def test_file_is_reread_on_every_get(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, "system.txt", "v1")
    provider = FilePromptProvider(
        files={SYSTEM_PROMPT_NAME: path},
        fallback=StaticPromptProvider(),
    )
    path.write_text("v2", encoding="utf-8")

    assert provider.get(SYSTEM_PROMPT_NAME) == "v2"


def test_name_without_a_file_falls_back(tmp_path: Path) -> None:
    provider = FilePromptProvider(files={}, fallback=StaticPromptProvider())

    assert provider.get(ROUTER_PROMPT_NAME) == StaticPromptProvider().get(ROUTER_PROMPT_NAME)


def test_unreadable_file_falls_back_with_a_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FilePromptProvider(
        files={SYSTEM_PROMPT_NAME: tmp_path / "missing.txt"},
        fallback=StaticPromptProvider(),
    )

    with caplog.at_level("WARNING"):
        prompt = provider.get(SYSTEM_PROMPT_NAME)

    assert prompt == StaticPromptProvider().get(SYSTEM_PROMPT_NAME)
    assert "prompt file unreadable" in caplog.text


def test_unknown_name_propagates_the_fallback_key_error(tmp_path: Path) -> None:
    provider = FilePromptProvider(files={}, fallback=StaticPromptProvider())

    with pytest.raises(KeyError):
        provider.get(UNKNOWN_NAME)
