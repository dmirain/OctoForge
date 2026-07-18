"""Message router: maps an incoming user message to process operations."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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
    """Ordered package of operations; an empty package is a passthrough."""

    ops: tuple[RouteOp, ...] = ()


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
        },
        "required": ["ops"],
    },
)

ROUTER_SYSTEM_PROMPT = (
    "You are the message router of a conversation.\n"
    "Active processes (limit {limit}):\n"
    "{processes}\n"
    "Decide what the user message means for these processes and ALWAYS answer with "
    "the route tool.\n"
    "Rules:\n"
    "1. A comment or detail about the current foreground work -> ops: [inject].\n"
    "2. A new independent question -> ops: [start_new].\n"
    "3. Cancel a process only on an explicit user request -> ops: [cancel(target_id)].\n"
    "4. 'Bring back task X' -> ops: [promote(target_id)].\n"
    "5. 'Stop everything' -> one cancel op per active process, optionally followed "
    "by start_new.\n"
    "6. Respect the limit: active processes minus your cancel ops plus one must not "
    "exceed the limit, otherwise do not emit start_new/promote."
)


class LLMRouter:
    """MessageRouter implementation doing a one-shot LLM tool call."""

    def __init__(self, llm: LLMClient, timeout_seconds: float) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        """Route the message; fall back to a deterministic decision on LLM trouble."""
        if not processes:
            return RouteDecision()
        try:
            reply = await asyncio.wait_for(
                self._llm.complete(
                    _build_messages(processes, message, max_processes),
                    tools=[ROUTE_TOOL_SPEC],
                ),
                timeout=self._timeout_seconds,
            )
        except Exception:  # router failures must not break the dialog
            return _fallback(processes)
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
        return RouteDecision(ops=ops)


def _build_messages(
    processes: tuple[ProcessInfo, ...],
    message: str,
    max_processes: int,
) -> list[ChatMessage]:
    lines = "\n".join(
        f"- id={process.id} | place={process.place.value} | title={process.title}"
        for process in processes
    )
    system = ROUTER_SYSTEM_PROMPT.format(limit=max_processes, processes=lines)
    return [
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=message),
    ]


def _fallback(processes: tuple[ProcessInfo, ...]) -> RouteDecision:
    """Deterministic decision used when the LLM answer is missing or unusable."""
    if any(process.place is ProcessPlace.FOREGROUND for process in processes):
        return RouteDecision(ops=(RouteOp(action=RouteAction.START_NEW),))
    return RouteDecision()


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
