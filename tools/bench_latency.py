#!/usr/bin/env python
"""Measure what OctoForge itself adds to an answer's latency.

    .venv/bin/python tools/bench_latency.py            # table
    .venv/bin/python tools/bench_latency.py --json      # machine-readable

The provider dominates end-to-end latency and says nothing about the framework
around it, so every scenario here runs the real stack — `ConversationManager`,
the actor, the persisted narrative, the LLM router, `AgentLoop` — against a
scripted in-process LLM whose timing is known exactly. What is left is ours:

* `dispatch`      submit() → the answer run's request reaches the LLM
                  (persist, route, branch assembly, spawn).
* `delivery`      a token leaving the LLM → the same token at a subscriber.
* `tools`         three 150 ms tool calls in one assistant message: they are
                  started as their arguments arrive and run concurrently, so
                  the round costs about one call, not three.
* `exchanges`     two questions asked back to back: both answers stream at the
                  same time, so the pair costs about one answer, not two.

Numbers land in README.md; keep the two in sync when this changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octoforge_core import (
    AgentLoop,
    ChatMessage,
    Completion,
    ConversationManager,
    Finished,
    MessageRole,
    TextDelta,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import StaticPromptProvider
from octoforge_core.agent.router import ROUTE_TOOL_NAME, LLMRouter
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.tasks.store import SqlAlchemyTaskStore
from octoforge_core.tools.base import ToolContext

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
    """Timestamps the scripted LLM and the bench tool leave behind."""

    request: list[float] = field(default_factory=list)
    token_emitted: list[float] = field(default_factory=list)
    tool_started: list[float] = field(default_factory=list)
    tool_finished: list[float] = field(default_factory=list)
    stream_finished: list[float] = field(default_factory=list)
    # the router only spends an LLM call when the dialog has live exchanges
    routed: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Script:
    """One scripted assistant message: text, tool calls, and their pacing."""

    tokens: int = ANSWER_TOKENS
    tool_calls: int = 0
    body_seconds: float = 0.0


class BenchLLM:
    """LLMClient whose timing is known: it records when it was asked and answered."""

    def __init__(self, scripts: list[Script], marks: Marks) -> None:
        self._scripts = list(scripts)
        self._marks = marks

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        """Answer the router: every message opens its own exchange."""
        self._marks.routed.append(time.perf_counter())
        call = ToolCall(
            id="route-call",
            name=ROUTE_TOOL_NAME,
            arguments={"action": "new", "exchange_id": None, "cancel_exchange_ids": []},
        )
        return Completion(
            message=ChatMessage(
                role=MessageRole.ASSISTANT, content="", tool_calls=(call,)
            )
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Replay the next script, timestamping the request and every token."""
        self._marks.request.append(time.perf_counter())
        script = self._scripts.pop(0) if len(self._scripts) > 1 else self._scripts[0]
        calls = tuple(
            ToolCall(id=f"call-{index}", name=TOOL_NAME, arguments={})
            for index in range(script.tool_calls)
        )
        # Arguments of a call finish streaming before the message does: the
        # loop may start it right away. That is the behavior under measurement.
        for call in calls:
            yield ToolCallReady(call=call)
        text = ""
        for index in range(script.tokens):
            if script.body_seconds:
                await asyncio.sleep(script.body_seconds / script.tokens)
            piece = f"token{index} "
            text += piece
            self._marks.token_emitted.append(time.perf_counter())
            yield LlmTextDelta(text=piece)
        self._marks.stream_finished.append(time.perf_counter())
        yield StreamFinished(
            message=ChatMessage(
                role=MessageRole.ASSISTANT, content=text, tool_calls=calls
            )
        )


class WaitTool:
    """Tool that costs a fixed, known amount of wall-clock time."""

    def __init__(self, marks: Marks) -> None:
        self._marks = marks
        self.spec = ToolSpec(
            name=TOOL_NAME,
            description="Wait for a fixed time (benchmark).",
            parameters_schema={"type": "object", "properties": {}},
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Sleep for the fixed latency, recording both ends."""
        self._marks.tool_started.append(time.perf_counter())
        await asyncio.sleep(TOOL_LATENCY)
        self._marks.tool_finished.append(time.perf_counter())
        return "done"


@dataclass(slots=True)
class Stack:
    """A live manager over a throwaway SQLite file, with its scripted LLM."""

    manager: ConversationManager
    marks: Marks
    engine: Any


async def build_stack(scripts: list[Script], directory: Path) -> Stack:
    """Assemble the real conversation stack around a scripted LLM."""
    marks = Marks()
    engine = create_engine(f"sqlite+aiosqlite:///{directory / 'bench.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    llm = BenchLLM(scripts, marks)
    registry = ToolRegistry()
    registry.register(WaitTool(marks))
    prompts = StaticPromptProvider()
    manager = ConversationManager(
        config=RunnerConfig(
            loop=AgentLoop(
                llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS
            ),
            prompts=prompts,
            router=LLMRouter(llm, timeout_seconds=ROUTER_TIMEOUT, prompts=prompts),
            max_processes=5,
            compactor=NoopContextCompactor(),
        ),
        dialogs=SqlAlchemyDialogRepository(session_factory),
        messages=SqlAlchemyMessageRepository(session_factory),
        tasks=SqlAlchemyTaskStore(session_factory),
        exchanges=SqlAlchemyExchangeRepository(session_factory),
    )
    return Stack(manager=manager, marks=marks, engine=engine)


async def _drain(events: asyncio.Queue[Any], finals: int) -> list[float]:
    """Collect delta arrival times until `finals` answers have finished."""
    arrivals: list[float] = []
    remaining = finals
    while remaining:
        payload = (await events.get()).payload
        if isinstance(payload, TextDelta):
            arrivals.append(time.perf_counter())
        elif isinstance(payload, Finished):
            remaining -= 1
    return arrivals


async def _one_answer(
    stack: Stack, user_id: str = USER_ID, question: str = QUESTION
) -> tuple[float, list[float]]:
    """Ask once; return (submit timestamp, delta arrival times)."""
    runner = await stack.manager.get_or_create_runner(user_id, CHANNEL)
    events = runner.subscribe()
    started = time.perf_counter()
    await runner.submit(question)
    arrivals = await _drain(events, finals=1)
    return started, arrivals


async def measure_dispatch_and_delivery(
    repeats: int, directory: Path
) -> dict[str, list[float]]:
    """Dispatch overhead (submit → LLM) and per-token delivery lag, in ms.

    One warm stack, one fresh dialog per repeat: reusing the same dialog would
    grow its narrative sample after sample and measure history assembly, while
    a fresh engine per repeat would measure SQLite file creation. Neither is
    the per-message cost this row is about.
    """
    dispatch: list[float] = []
    delivery: list[float] = []
    stack = await build_stack([Script()], directory / "dd")
    try:
        for index in range(repeats):
            emitted_before = len(stack.marks.token_emitted)
            started, arrivals = await _one_answer(stack, f"{USER_ID}-{index}")
            dispatch.append((stack.marks.request[-1] - started) * MS)
            emitted = stack.marks.token_emitted[emitted_before:]
            lags = [
                (arrival - emit) * MS
                for emit, arrival in zip(emitted, arrivals, strict=False)
            ]
            delivery.append(statistics.median(lags))
    finally:
        await stack.manager.stop_all()
        await stack.engine.dispose()
    return {"dispatch": dispatch, "delivery": delivery}


async def measure_tools(repeats: int, directory: Path) -> list[float]:
    """Wall-clock cost of three 150 ms tool calls in one message, in ms."""
    elapsed: list[float] = []
    for index in range(repeats):
        # first message calls the tools, the second one answers
        scripts = [Script(tokens=2, tool_calls=TOOL_CALLS_PER_MESSAGE), Script()]
        stack = await build_stack(scripts, directory / f"tools-{index}")
        try:
            await _one_answer(stack)
            marks = stack.marks
            elapsed.append((max(marks.tool_finished) - min(marks.tool_started)) * MS)
        finally:
            await stack.manager.stop_all()
            await stack.engine.dispose()
    return elapsed


async def measure_exchanges(repeats: int, directory: Path) -> dict[str, list[float]]:
    """Two questions in a row: total wall clock, and the routed dispatch of the second.

    The second question arrives while the first answer is still streaming, so
    this is the path that does spend a router call — the `routing` row is the
    framework's share of it (prompt build, live-exchange snapshot, decision
    parse, spawn), with the scripted router answering instantly.
    """
    elapsed: list[float] = []
    routing: list[float] = []
    for index in range(repeats):
        stack = await build_stack(
            [Script(body_seconds=RUN_LATENCY)], directory / f"ex-{index}"
        )
        try:
            runner = await stack.manager.get_or_create_runner(USER_ID, CHANNEL)
            events = runner.subscribe()
            started = time.perf_counter()
            await runner.submit("First question, please take your time.")
            requests_before = len(stack.marks.request)
            second = time.perf_counter()
            await runner.submit("Second, unrelated question.")
            await _drain(events, finals=2)
            elapsed.append((time.perf_counter() - started) * MS)
            routing.append((stack.marks.request[requests_before] - second) * MS)
        finally:
            await stack.manager.stop_all()
            await stack.engine.dispose()
    return {"exchanges": elapsed, "routing": routing}


def summarize(samples: list[float]) -> dict[str, float]:
    """Median, p90 and min of a sample set, rounded to two decimals."""
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return {
        "median_ms": round(statistics.median(ordered), 2),
        "p90_ms": round(p90, 2),
        "min_ms": round(ordered[0], 2),
    }


async def run(repeats: int) -> dict[str, dict[str, float]]:
    """Run every scenario and summarize it."""
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "dd").mkdir(parents=True)
        for child in ("tools", "ex"):
            for index in range(repeats):
                (directory / f"{child}-{index}").mkdir(parents=True)
        pair = await measure_dispatch_and_delivery(repeats, directory)
        tools = await measure_tools(repeats, directory)
        concurrent = await measure_exchanges(repeats, directory)
    return {
        "dispatch": summarize(pair["dispatch"]),
        "routing": summarize(concurrent["routing"]),
        "delivery": summarize(pair["delivery"]),
        "tools": summarize(tools),
        "exchanges": summarize(concurrent["exchanges"]),
    }


BASELINES = {
    "dispatch": "submit() -> the provider is asked, warm dialog, nothing else running",
    "routing": "same, for a message arriving while an answer is still streaming",
    "delivery": "provider token -> subscriber",
    "tools": (
        f"3 x {int(TOOL_LATENCY * MS)} ms tools, "
        f"{int(TOOL_CALLS_PER_MESSAGE * TOOL_LATENCY * MS)} ms if serial"
    ),
    "exchanges": f"2 x {int(RUN_LATENCY * MS)} ms answers, {int(2 * RUN_LATENCY * MS)} ms if queued",
}


def print_table(results: dict[str, dict[str, float]], repeats: int) -> None:
    """Print the summary table."""
    print(f"OctoForge latency, {repeats} runs each (scripted LLM, real stack)\n")
    print(f"{'scenario':<12} {'median':>9} {'p90':>9}   baseline")
    for name, stats in results.items():
        median = f"{stats['median_ms']:.2f} ms"
        p90 = f"{stats['p90_ms']:.2f} ms"
        print(f"{name:<12} {median:>9} {p90:>9}   {BASELINES[name]}")


def main() -> None:
    """Parse arguments, run the scenarios, print the results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--json", action="store_true", help="print raw JSON instead of a table"
    )
    args = parser.parse_args()
    results = asyncio.run(run(args.repeats))
    if args.json:
        print(json.dumps(results, indent=2))
        return
    print_table(results, args.repeats)


if __name__ == "__main__":
    main()
