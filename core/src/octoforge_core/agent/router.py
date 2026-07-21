"""Message router: maps an incoming user message to process operations."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, PromptProvider
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.skills.base import SkillSpec


class RouteAction(StrEnum):
    """Operations the router can request for a user message."""

    INJECT = "inject"
    START_NEW = "start_new"
    CANCEL = "cancel"
    PROMOTE = "promote"


@dataclass(frozen=True, slots=True)
class RouteOp:
    """One routing operation; CANCEL/PROMOTE require a target process id."""

    action: RouteAction
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Ordered package of operations; an empty package is a passthrough.

    `searches` are free-text skill pre-search queries about the message's
    intent; the actor runs them and injects the found scenarios into the new
    process branch.
    """

    ops: tuple[RouteOp, ...] = ()
    searches: tuple[str, ...] = ()


class ProcessPlace(StrEnum):
    """Where an active process currently sits."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """Snapshot of an active process handed to the router."""

    id: str
    title: str
    place: ProcessPlace


class MessageRouter(Protocol):
    """Decides what an incoming user message means for the active processes."""

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        """Return the package of operations to apply for the message."""
        ...


ROUTE_TOOL_NAME = "route"
MAX_SEARCHES = 3
ROUTE_TOOL_SPEC = SkillSpec(
    name=ROUTE_TOOL_NAME,
    description="Route the user message to the dialog processes.",
    parameters_schema={
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "description": "Ordered routing operations to apply.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [action.value for action in RouteAction],
                        },
                        "target_id": {
                            "type": ["string", "null"],
                            "description": "Target process id for cancel/promote; null otherwise.",
                        },
                    },
                    "required": ["action", "target_id"],
                },
            },
            "searches": {
                "type": "array",
                "description": (
                    "Free-text skill search queries capturing the message's intents; "
                    "empty for pure chit-chat."
                ),
                "items": {"type": "string"},
                "maxItems": MAX_SEARCHES,
            },
        },
        "required": ["ops"],
    },
)


class LLMRouter:
    """MessageRouter implementation doing a one-shot LLM tool call.

    The system prompt template comes from the injected `PromptProvider`
    (`ROUTER_PROMPT_NAME`); it may use the `{limit}` and `{processes}`
    placeholders.
    """

    def __init__(self, llm: LLMClient, timeout_seconds: float, prompts: PromptProvider) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds
        self._prompts = prompts

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        """Route the message; fall back to a deterministic decision on LLM trouble.

        The LLM is called even with an empty process snapshot: the decision
        also carries the skill pre-search queries for the new process.
        """
        try:
            completion = await asyncio.wait_for(
                self._llm.complete(
                    self._build_messages(processes, message, max_processes),
                    tools=[ROUTE_TOOL_SPEC],
                ),
                timeout=self._timeout_seconds,
            )
        except Exception:  # router failures must not break the dialog
            return _fallback(processes)
        reply = completion.message
        call = next((c for c in reply.tool_calls if c.name == ROUTE_TOOL_NAME), None)
        if call is None:
            return _fallback(processes)
        known_ids = {process.id for process in processes}
        raw_ops = call.arguments.get("ops")
        ops = tuple(
            op
            for raw in (raw_ops if isinstance(raw_ops, list) else [])
            if (op := _parse_op(raw, known_ids)) is not None
        )
        return RouteDecision(
            ops=_resolve_conflicts(ops),
            searches=_parse_searches(call.arguments.get("searches")),
        )

    def _build_messages(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> list[ChatMessage]:
        lines = "\n".join(
            f"- id={process.id} | place={process.place.value} | title={process.title}"
            for process in processes
        )
        template = self._prompts.get(ROUTER_PROMPT_NAME)
        system = template.format(limit=max_processes, processes=lines)
        return [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=message),
        ]


def _fallback(processes: tuple[ProcessInfo, ...]) -> RouteDecision:
    """Deterministic decision used when the LLM answer is missing or unusable."""
    if any(process.place is ProcessPlace.FOREGROUND for process in processes):
        return RouteDecision(ops=(RouteOp(action=RouteAction.INJECT),))
    return RouteDecision()


def _resolve_conflicts(ops: tuple[RouteOp, ...]) -> tuple[RouteOp, ...]:
    """Drop start_new ops when the message is injected into the foreground run."""
    if any(op.action is RouteAction.INJECT for op in ops):
        return tuple(op for op in ops if op.action is not RouteAction.START_NEW)
    return ops


def _parse_searches(raw: object) -> tuple[str, ...]:
    """Keep only non-empty string queries, capped at MAX_SEARCHES."""
    if not isinstance(raw, list):
        return ()
    queries = tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())
    return queries[:MAX_SEARCHES]


def _parse_op(raw: object, known_ids: set[str]) -> RouteOp | None:
    """Validate one raw op; invalid ops are dropped, not reported."""
    if not isinstance(raw, dict):
        return None
    try:
        action = RouteAction(str(raw.get("action")))
    except ValueError:
        return None
    target = raw.get("target_id")
    if action in (RouteAction.INJECT, RouteAction.START_NEW):
        return RouteOp(action=action) if target is None else None
    if not isinstance(target, str) or target not in known_ids:
        return None
    return RouteOp(action=action, target_id=target)
