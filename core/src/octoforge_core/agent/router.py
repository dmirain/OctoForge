"""Message router: maps an incoming user message to process operations."""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, PromptProvider
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolSpec

logger = logging.getLogger(__name__)


class RouteAction(StrEnum):
    """Operations the router can request for a user message."""

    INJECT = "inject"
    START_NEW = "start_new"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class RouteOp:
    """One routing operation; CANCEL requires a target process id."""

    action: RouteAction
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Ordered package of operations; an empty package is the default route.

    The runner maps an empty package to a START_NEW op (`agent/runner.py`),
    so a message the router had nothing to say about still starts a new
    foreground process — it is never dropped as a no-op passthrough.
    """

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
ROUTE_TOOL_SPEC = ToolSpec(
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
                            "description": (
                                "Process id to cancel; ignored for inject and start_new "
                                "(pass null there)."
                            ),
                        },
                    },
                    "required": ["action", "target_id"],
                },
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
        """Route the message; fall back to a deterministic decision on LLM trouble."""
        if not processes:
            # No active processes: the decision is trivially START_NEW (the
            # runner maps an empty package to it), so the LLM call is skipped.
            return RouteDecision()
        try:
            completion = await asyncio.wait_for(
                self._llm.complete(
                    self._build_messages(processes, message, max_processes),
                    tools=[ROUTE_TOOL_SPEC],
                ),
                timeout=self._timeout_seconds,
            )
        except Exception:  # router failures must not break the dialog
            logger.warning("router LLM call failed; falling back", exc_info=True)
            return _fallback(processes)
        reply = completion.message
        call = next((c for c in reply.tool_calls if c.name == ROUTE_TOOL_NAME), None)
        if call is None:
            logger.warning("router answer carried no %s call; falling back", ROUTE_TOOL_NAME)
            return _fallback(processes)
        known_ids = {process.id for process in processes}
        raw_ops = call.arguments.get("ops")
        ops = tuple(
            op
            for raw in (raw_ops if isinstance(raw_ops, list) else [])
            if (op := _parse_op(raw, known_ids)) is not None
        )
        return RouteDecision(ops=_resolve_conflicts(ops))

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
    """Resolve contradictions: inject drops start_new; keep at most one op of each."""
    deduped = _dedupe_singletons(ops)
    if any(op.action is RouteAction.INJECT for op in deduped):
        return tuple(op for op in deduped if op.action is not RouteAction.START_NEW)
    return deduped


def _dedupe_singletons(ops: tuple[RouteOp, ...]) -> tuple[RouteOp, ...]:
    """Keep only the first start_new and the first inject op of the package."""
    seen: set[RouteAction] = set()
    result: list[RouteOp] = []
    for op in ops:
        if op.action in (RouteAction.START_NEW, RouteAction.INJECT):
            if op.action in seen:
                continue
            seen.add(op.action)
        result.append(op)
    return tuple(result)


def _parse_op(raw: object, known_ids: set[str]) -> RouteOp | None:
    """Validate one raw op; an unusable op is dropped (and logged, never silent).

    `target_id` is only meaningful for cancel, which needs a known process to
    stop. For inject and start_new the field is ignored whatever it holds:
    injection is the pull model (the message already lives in the narrative and
    every running process re-syncs it — `ConversationRunner._apply_inject` never
    looks at a target), so a model that helpfully fills in the id of the process
    it means still expresses a valid decision. Dropping the whole op over that
    field turned a correct inject into an empty package, i.e. START_NEW — the
    opposite routing.
    """
    if not isinstance(raw, dict):
        logger.warning("router op dropped, not an object: %r", raw)
        return None
    try:
        action = RouteAction(str(raw.get("action")))
    except ValueError:
        logger.warning("router op dropped, unknown action: %r", raw)
        return None
    target = raw.get("target_id")
    if action in (RouteAction.INJECT, RouteAction.START_NEW):
        return RouteOp(action=action)
    if not isinstance(target, str) or target not in known_ids:
        logger.warning("router op dropped, %s needs a known target: %r", action.value, raw)
        return None
    return RouteOp(action=action, target_id=target)
