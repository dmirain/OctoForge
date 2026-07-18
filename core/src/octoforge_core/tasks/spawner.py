"""Port for spawning background tasks as dialog processes."""

from typing import Protocol


class TaskSpawner(Protocol):
    """Creates a background task for the current dialog and starts its process."""

    async def spawn(self, title: str, prompt: str) -> str:
        """Spawn the task and return a confirmation text, or a refusal (e.g. limit)."""
        ...
