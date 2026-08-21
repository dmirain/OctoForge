"""Latency benchmark scenarios and statistical summaries."""

import statistics
import tempfile
import time
from pathlib import Path

from bench_stack import build_stack, drain, one_answer
from bench_types import (
    CHANNEL,
    MS,
    RUN_LATENCY,
    TOOL_CALLS_PER_MESSAGE,
    USER_ID,
    Script,
)
from octoforge_core import DialogSubmission


async def measure_dispatch_and_delivery(
    repeats: int,
    directory: Path,
) -> dict[str, list[float]]:
    dispatch: list[float] = []
    delivery: list[float] = []
    stack = await build_stack([Script()], directory / "dd")
    try:
        for index in range(repeats):
            emitted_before = len(stack.marks.token_emitted)
            started, arrivals = await one_answer(stack, f"{USER_ID}-{index}")
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
    elapsed: list[float] = []
    for index in range(repeats):
        stack = await build_stack(
            [Script(tokens=2, tool_calls=TOOL_CALLS_PER_MESSAGE), Script()],
            directory / f"tools-{index}",
        )
        try:
            await one_answer(stack)
            elapsed.append(
                (max(stack.marks.tool_finished) - min(stack.marks.tool_started)) * MS
            )
        finally:
            await stack.manager.stop_all()
            await stack.engine.dispose()
    return elapsed


async def measure_exchanges(repeats: int, directory: Path) -> dict[str, list[float]]:
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
            await runner.submit(
                DialogSubmission("First question, please take your time.")
            )
            requests_before = len(stack.marks.request)
            second = time.perf_counter()
            await runner.submit(DialogSubmission("Second, unrelated question."))
            await drain(events, 2)
            elapsed.append((time.perf_counter() - started) * MS)
            routing.append((stack.marks.request[requests_before] - second) * MS)
        finally:
            await stack.manager.stop_all()
            await stack.engine.dispose()
    return {"exchanges": elapsed, "routing": routing}


def summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return {
        "median_ms": round(statistics.median(ordered), 2),
        "p90_ms": round(p90, 2),
        "min_ms": round(ordered[0], 2),
    }


async def run(repeats: int) -> dict[str, dict[str, float]]:
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
