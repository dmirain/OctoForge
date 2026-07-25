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


def test_system_prompt_puts_retrieval_before_action() -> None:
    """The observed failure mode is answering from the model's own head.

    The prompt must therefore make retrieval the first step, bind the agent to
    what it finds, and forbid trial-and-error — the three rules that pull it
    towards the store instead of improvisation.
    """
    assert "FIRST step is instruction_search" in DEFAULT_SYSTEM_PROMPT
    # memories merged into the one search: the prompt must sell it as covering them
    assert "memories" in DEFAULT_SYSTEM_PROMPT
    assert "memory_search" not in DEFAULT_SYSTEM_PROMPT
    assert "binding" in DEFAULT_SYSTEM_PROMPT
    assert "trial and error" in DEFAULT_SYSTEM_PROMPT
    # searching must read as cheap, or the model keeps optimizing it away
    assert "cheap" in DEFAULT_SYSTEM_PROMPT
    # assertion-shaped trigger: identity/installation questions look like small
    # talk, so without this clause the model answers them from its own head
    assert "search before asserting" in DEFAULT_SYSTEM_PROMPT
    assert "say you do not know" in DEFAULT_SYSTEM_PROMPT


def test_system_prompt_holds_meta_rules_only() -> None:
    assert "instruction_search" in DEFAULT_SYSTEM_PROMPT
    assert "instruction_save" in DEFAULT_SYSTEM_PROMPT
    assert "System service notes" in DEFAULT_SYSTEM_PROMPT
    assert "finished background task" not in DEFAULT_SYSTEM_PROMPT
    assert "pipe tables" in DEFAULT_SYSTEM_PROMPT
    # per-tool rules moved into the system skill scenarios
    assert "task_create" not in DEFAULT_SYSTEM_PROMPT
    assert "memory_store" not in DEFAULT_SYSTEM_PROMPT
    assert "http_request" not in DEFAULT_SYSTEM_PROMPT
    assert "cron_pause" not in DEFAULT_SYSTEM_PROMPT
