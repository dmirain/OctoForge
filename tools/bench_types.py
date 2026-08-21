"""Shared constants, scripts, marks and stack values for latency benchmarks."""

from dataclasses import dataclass, field

from octoforge_core import ConversationManager
from sqlalchemy.ext.asyncio import AsyncEngine

USER_ID = "bench-user"
CHANNEL = "bench"
QUESTION = "How long does this take?"
ANSWER_TOKENS = 12
TOOL_NAME = "bench_wait"
TOOL_LATENCY = 0.15
TOOL_CALLS_PER_MESSAGE = 3
RUN_LATENCY = 0.4
ROUTER_TIMEOUT = 5.0
MAX_ITERATIONS = 5
DEFAULT_REPEATS = 15
MS = 1000.0


@dataclass(slots=True)
class Marks:
    request: list[float] = field(default_factory=list)
    token_emitted: list[float] = field(default_factory=list)
    tool_started: list[float] = field(default_factory=list)
    tool_finished: list[float] = field(default_factory=list)
    stream_finished: list[float] = field(default_factory=list)
    routed: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Script:
    tokens: int = ANSWER_TOKENS
    tool_calls: int = 0
    body_seconds: float = 0.0


@dataclass(slots=True)
class Stack:
    manager: ConversationManager
    marks: Marks
    engine: AsyncEngine
