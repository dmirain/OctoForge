"""Mutable state and value objects of one agent process."""

import asyncio
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import AssistantMessage, LoopEvent
from octoforge_core.dialogs.api import Exchange
from octoforge_core.domain import ChatMessage
from octoforge_core.tariffs.api import UsageOrigin
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus


@dataclass(frozen=True, slots=True)
class AnswerSource:
    message_id: str | None
    client_message_id: str | None
    exchange_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessTaskDraft:
    title: str
    prompt: str
    kind: TaskKind
    cron_job_id: str | None = None
    source: AnswerSource | None = None


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    exchange: Exchange
    message: ChatMessage
    client_key: str | None = None
    cancel_epoch: int | None = None
    notify_limit: bool = True


@dataclass(frozen=True, slots=True)
class OwnerRequest:
    exchange_id: str
    message: ChatMessage
    client_key: str | None = None
    cancelled: frozenset[str] = frozenset()
    cancel_epoch: int | None = None
    known: Exchange | None = None

    def to_answer_request(self, exchange: Exchange) -> AnswerRequest:
        return AnswerRequest(
            exchange,
            self.message,
            self.client_key,
            self.cancel_epoch,
        )


@dataclass(slots=True)
class Process:
    id: str
    title: str
    task_id: str
    control: LoopControl
    branch: list[ChatMessage]
    pump: asyncio.Task[None] | None = None
    narrative_built: bool = False
    synced_len: int = 0
    watermark: int = 0
    exchange_id: str | None = None
    source_message_id: str | None = None
    source_client_message_id: str | None = None
    terminal_accepted: bool = False
    overflow_retried: bool = False
    asked: bool = False
    origin: UsageOrigin = UsageOrigin.INTERACTIVE
    spent_prompt: int = 0
    spent_completion: int = 0


@dataclass(frozen=True, slots=True)
class PumpOutcome:
    status: TaskStatus
    terminal: LoopEvent
    task: Task | None = None


def observe_spend(process: Process, event: LoopEvent) -> None:
    if isinstance(event, AssistantMessage) and event.usage is not None:
        process.spent_prompt += event.usage.prompt_tokens
        process.spent_completion += event.usage.completion_tokens


def task_source_message(task: Task) -> str | None:
    raw = task.input.get("source_message_id")
    return raw if isinstance(raw, str) else None


def task_client_source(task: Task) -> str | None:
    raw = task.input.get("source_client_message_id")
    return raw if isinstance(raw, str) else None


def task_origin(task: Task) -> UsageOrigin:
    if task.kind is TaskKind.ANSWER:
        return UsageOrigin.INTERACTIVE
    if "cron_job_id" in task.input:
        return UsageOrigin.CRON
    return UsageOrigin.BACKGROUND
