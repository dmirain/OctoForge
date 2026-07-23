"""Tests for the PromptProvider port and its built-in StaticPromptProvider."""

import pytest

from octoforge_core.agent.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    ROUTER_PROMPT_NAME,
    ROUTER_SYSTEM_PROMPT,
    SYSTEM_PROMPT_NAME,
    StaticPromptProvider,
)

CUSTOM_PROMPT = "custom prompt text"
UNKNOWN_NAME = "no-such-prompt"


def test_static_provider_serves_the_built_in_defaults() -> None:
    provider = StaticPromptProvider()

    assert provider.get(SYSTEM_PROMPT_NAME) == DEFAULT_SYSTEM_PROMPT
    assert provider.get(ROUTER_PROMPT_NAME) == ROUTER_SYSTEM_PROMPT


def test_static_provider_serves_a_custom_mapping() -> None:
    provider = StaticPromptProvider({SYSTEM_PROMPT_NAME: CUSTOM_PROMPT})

    assert provider.get(SYSTEM_PROMPT_NAME) == CUSTOM_PROMPT


def test_static_provider_raises_for_an_unknown_name() -> None:
    provider = StaticPromptProvider()

    with pytest.raises(KeyError):
        provider.get(UNKNOWN_NAME)


def test_system_prompt_holds_meta_rules_only() -> None:
    assert "instruction_search" in DEFAULT_SYSTEM_PROMPT
    assert "instruction_save" in DEFAULT_SYSTEM_PROMPT
    assert "finished background task" in DEFAULT_SYSTEM_PROMPT
    assert "pipe tables" in DEFAULT_SYSTEM_PROMPT
    # per-tool rules moved into the system skill scenarios
    assert "task_create" not in DEFAULT_SYSTEM_PROMPT
    assert "memory_store" not in DEFAULT_SYSTEM_PROMPT
    assert "http_request" not in DEFAULT_SYSTEM_PROMPT
    assert "cron_pause" not in DEFAULT_SYSTEM_PROMPT
