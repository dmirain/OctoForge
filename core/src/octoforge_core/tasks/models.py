"""Domain objects for background tasks."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from octoforge_core.time import utc_now


class TaskKind(StrEnum):
    """Kinds of background work."""

    SKILL = "skill"
    PROMPT = "prompt"


class TaskStatus(StrEnum):
    """Lifecycle states of a task."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Task:
    """A background unit of work spawned by the agent."""

    conversation_id: str
    title: str
    kind: TaskKind
    input: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
