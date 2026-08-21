#!/usr/bin/env python
"""Measure OctoForge dispatch, delivery, tool and concurrent-answer latency."""

import argparse
import asyncio
import json

from bench_scenarios import run
from bench_types import (
    DEFAULT_REPEATS,
    MS,
    RUN_LATENCY,
    TOOL_CALLS_PER_MESSAGE,
    TOOL_LATENCY,
)

BASELINES = {
    "dispatch": "submit() -> provider, warm dialog",
    "routing": "message arriving while an answer streams",
    "delivery": "provider token -> subscriber",
    "tools": (
        f"3 x {int(TOOL_LATENCY * MS)} ms tools, "
        f"{int(TOOL_CALLS_PER_MESSAGE * TOOL_LATENCY * MS)} ms if serial"
    ),
    "exchanges": f"2 x {int(RUN_LATENCY * MS)} ms answers if concurrent",
}


def print_table(results: dict[str, dict[str, float]], repeats: int) -> None:
    print(f"OctoForge latency, {repeats} runs each (scripted LLM, real stack)\n")
    print(f"{'scenario':<12} {'median':>9} {'p90':>9}   baseline")
    for name, stats in results.items():
        median = f"{stats['median_ms']:.2f} ms"
        p90 = f"{stats['p90_ms']:.2f} ms"
        print(f"{name:<12} {median:>9} {p90:>9}   {BASELINES[name]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = asyncio.run(run(args.repeats))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.repeats)


if __name__ == "__main__":
    main()
