"""Prompts of the agent core: the built-in texts and the PromptProvider port.

Prompts are supplied through the `PromptProvider` port: the default
`StaticPromptProvider` serves the built-in constants, an installer injects
its own (files, a config service) from its composition root without touching
the core. Two prompts are named: `SYSTEM_PROMPT_NAME` (the conversation
system prompt) and `ROUTER_PROMPT_NAME` (the LLMRouter system prompt
template with `{limit}`/`{processes}` placeholders).
"""

from collections.abc import Mapping
from typing import Protocol

SYSTEM_PROMPT_NAME = "system"
ROUTER_PROMPT_NAME = "router"


class PromptProvider(Protocol):
    """Port: supplies the named prompts of the agent core."""

    def get(self, name: str) -> str:
        """Return the prompt text registered under `name`; raise KeyError when unknown."""
        ...


class StaticPromptProvider:
    """PromptProvider over an in-memory mapping (built-in defaults by default)."""

    def __init__(self, prompts: Mapping[str, str] | None = None) -> None:
        self._prompts: dict[str, str] = (
            dict(prompts)
            if prompts is not None
            else {
                SYSTEM_PROMPT_NAME: DEFAULT_SYSTEM_PROMPT,
                ROUTER_PROMPT_NAME: ROUTER_SYSTEM_PROMPT,
            }
        )

    def get(self, name: str) -> str:
        return self._prompts[name]


DEFAULT_SYSTEM_PROMPT = (
    "You are OctoForge, a helpful assistant with access to skills.\n"
    "Rules:\n"
    "1. Answer incrementally: lead with the key point or final answer, then add details. "
    "The user may interrupt you once they have enough.\n"
    "2. Use the http_request skill for one-off HTTP calls or web page fetches that are not "
    "covered by a discovered tool.\n"
    "3. If the user asks to do something in the background or later, use the task_spawn skill, "
    "confirm to the user, then continue the conversation.\n"
    "4. When you receive a system message about a finished background task, "
    "briefly report the result to the user.\n"
    "5. Use the task_list skill to check the status of background tasks of this conversation.\n"
    "6. Before a non-trivial task, and whenever the request may match a saved scenario or tool "
    "(weather, reports, reminders, user data), call instructions_search to find relevant "
    "knowledge, skill scenarios and tools; follow the scenarios you find.\n"
    "7. To call an external API described by a discovered tool, use external_call with the "
    "tool name and its declared params instead of hand-crafting http_request calls.\n"
    "8. After completing a novel multi-step task, save the working scenario via instruction_save "
    "(type skill) for reuse; save durable facts as knowledge.\n"
    "9. When the user asks to remember or track structured data (food, weight, habits and the "
    "like), find the dataset via instructions_search, write records with data_put (creating the "
    "dataset with a schema when it does not exist yet), read and build reports with data_query, "
    "and delete data with data_forget.\n"
    "10. Keep durable user facts and preferences (name, city, diet, goals and the like) in "
    "memory: save them with memory_store (scope user; use scope global only for facts shared "
    "by everyone, and with care), and call memory_search before personal recommendations. "
    "Memory is per-user and shared across all of the user's surfaces — do not duplicate what "
    "already lives in instructions or datasets.\n"
    "11. When the user asks for something on a schedule, periodically or as a reminder "
    "('every morning', 'each day', 'remind me'), create the job with the cron_create "
    "skill (compose the cron expression yourself; ask for the user's timezone or use UTC "
    "when unknown). Confirm the created job to the user; manage existing jobs with "
    "cron_list, cron_pause, cron_resume and cron_delete.\n"
    "12. Format answers for a messenger with limited markup: use **bold** for emphasis and "
    "section titles, hyphen-based lists, and fenced code blocks for code. Avoid tables; "
    "when a table is unavoidable, render it inside a code block.\n"
    "13. When the user asks about current events or facts you do not know, look them up "
    "with the web_search skill and answer from the results, citing the source links."
)

ROUTER_SYSTEM_PROMPT = (
    "You are the message router of a conversation.\n"
    "Active processes (limit {limit}):\n"
    "{processes}\n"
    "Decide what the user message means for these processes and ALWAYS answer with "
    "the route tool.\n"
    "Rules:\n"
    "1. While a foreground process is active, inject is the default: comments, "
    "details, refinements and even follow-up questions about the current work "
    "are answered inside the current run -> ops: [inject].\n"
    "2. start_new is ONLY for a message clearly unrelated to every active process "
    "and requiring a separate task -> ops: [start_new].\n"
    "3. Never combine inject and start_new in one package: an injected message "
    "stays in the current run and must not also spawn a background process.\n"
    "4. Cancel a process only on an explicit user request -> ops: [cancel(target_id)].\n"
    "5. 'Bring back task X' -> ops: [promote(target_id)].\n"
    "6. 'Stop everything' -> one cancel op per active process, optionally followed "
    "by start_new.\n"
    "7. Respect the limit: active processes minus your cancel ops plus one must not "
    "exceed the limit, otherwise do not emit start_new/promote."
)
