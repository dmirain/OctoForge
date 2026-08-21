"""Named prompt provider and built-in prompt registry."""

from collections.abc import Mapping
from typing import Protocol

from octoforge_core.agent.prompt_text import DEFAULT_SYSTEM_PROMPT, ROUTER_SYSTEM_PROMPT

SYSTEM_PROMPT_NAME = "system"
ROUTER_PROMPT_NAME = "router"


class PromptProvider(Protocol):
    """Port supplying named prompts to the agent core."""

    def get(self, name: str) -> str:
        """Return a registered prompt, or raise KeyError when unknown."""
        ...


class StaticPromptProvider:
    """In-memory prompt provider using the built-in texts by default."""

    def __init__(self, prompts: Mapping[str, str] | None = None) -> None:
        self._prompts = (
            dict(prompts)
            if prompts is not None
            else {
                SYSTEM_PROMPT_NAME: DEFAULT_SYSTEM_PROMPT,
                ROUTER_PROMPT_NAME: ROUTER_SYSTEM_PROMPT,
            }
        )

    def get(self, name: str) -> str:
        return self._prompts[name]
