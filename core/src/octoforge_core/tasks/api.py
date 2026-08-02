"""Public boundary of the tasks module.

Domain objects and errors of background tasks. Convention of every module:
DTOs and errors live in `api.py`, ORM rows in `models.py`, the port and its
implementations in `store.py` (the 2026-07-27 boundary audit brought tasks
in line — it was the one module with the layout inverted).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from octoforge_core.time import utc_now


class TaskKind(StrEnum):
    """Kinds of background work.

    RUN is user-visible deferred work; ANSWER is the internal mechanics of
    answering a user message (hidden from the task tools).
    """

    RUN = "run"
    ANSWER = "answer"


class TaskStatus(StrEnum):
    """Lifecycle states of a task."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Task:
    """A background unit of work spawned by the agent."""

    dialog_id: str
    title: str
    kind: TaskKind
    input: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # the obligation this run is paying; None for RUN tasks, which owe the
    # user nothing, and on rows written before the column existed
    exchange_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    delivered_at: datetime | None = None


class TaskNotFoundError(Exception):
    """Raised when a task id is not present in the store."""
