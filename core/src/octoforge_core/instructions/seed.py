"""Seed data for an empty instructions store: a generic HTTP tool plus example skills.

Seeding is data, not code: the baseline records are regular instructions
written through the facade, so they can be edited or deleted like any other.
"""

import json
from dataclasses import dataclass

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)

SEED_WEATHER_TOOL_TITLE = "wttr_in_weather"


@dataclass(frozen=True, slots=True)
class SeedInstruction:
    """One baseline record written by `seed_if_empty`."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]


SEED_INSTRUCTIONS: tuple[SeedInstruction, ...] = (
    SeedInstruction(
        kind=InstructionType.TOOL,
        title=SEED_WEATHER_TOOL_TITLE,
        content=json.dumps(
            {
                "method": "GET",
                "url_template": "https://wttr.in/{city}?format=j2",
                "params_schema": {"city": {"type": "string", "required": True}},
                "auth": "none",
            }
        ),
        tags=("http", "weather", "example"),
    ),
    SeedInstruction(
        kind=InstructionType.SKILL,
        title="get_current_weather",
        content=(
            "Scenario: report the current weather in a city.\n"
            "1. Call external_call with name 'wttr_in_weather' and params "
            '{"city": "<city>"}.\n'
            "2. From the JSON answer take current_condition[0]: temp_C, FeelsLikeC, "
            "weatherDesc[0].value, humidity.\n"
            "3. Answer the user with a short summary in the user's language."
        ),
        tags=("weather", "scenario", "example"),
    ),
    SeedInstruction(
        kind=InstructionType.SKILL,
        title="compare_weather_two_cities",
        content=(
            "Scenario: compare the weather in two cities.\n"
            "1. Call external_call with name 'wttr_in_weather' for the first city, "
            "then for the second one.\n"
            "2. Take temp_C and weatherDesc[0].value from each JSON answer.\n"
            "3. Report both cities and the temperature difference."
        ),
        tags=("weather", "scenario", "example"),
    ),
)


async def seed_if_empty(service: InstructionService) -> None:
    """Write the baseline records unless they are already present.

    The seed tool record acts as the marker: if it exists, seeding already
    happened and the call is a no-op. The check goes through the facade, so it
    keeps working when the module is extracted behind an HTTP boundary.
    """
    try:
        await service.get_by_name(SEED_WEATHER_TOOL_TITLE, InstructionType.TOOL)
        return
    except InstructionNotFoundError:
        pass
    for seed in SEED_INSTRUCTIONS:
        await service.save(seed.kind, seed.title, seed.content, seed.tags)
