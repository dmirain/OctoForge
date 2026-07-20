"""Seed data for an empty instructions store: a generic HTTP tool plus example skills.

Seeding is data, not code: the baseline records are regular instructions
written through the facade, so they can be edited or deleted like any other.
"""

import json
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)

SEED_WEATHER_TOOL_TITLE = "wttr_in_weather"
SEED_CRON_HTTP_TOOL_TITLES = (
    "cron_create_job",
    "cron_list_jobs",
    "cron_delete_job",
    "cron_pause_job",
    "cron_resume_job",
)
SEED_CRON_SCENARIO_TITLE = "schedule_a_recurring_report"
SEED_CRON_SCENARIO_CONTENT = (
    "Scenario: schedule a recurring report or a one-time reminder.\n"
    "1. Compose the cron expression for the requested cadence "
    "(e.g. '0 9 * * *' for every day at 09:00) and the user's IANA timezone "
    "(ask when unknown; use 'UTC' when unclear). For a one-time reminder "
    "include the date fields (minute hour day-of-month month *) with the "
    "nearest future occurrence.\n"
    "2. Call the cron_create skill with title, schedule, prompt and timezone; "
    "the prompt is the instruction you will receive on every firing. For a "
    "one-time reminder pass one_shot=true — the job deletes itself after it "
    "fires; never use task_spawn for reminders.\n"
    "3. Confirm the created job to the user: title, schedule, timezone and "
    "the next fire time from the result. Manage jobs later with the "
    "cron_list, cron_pause, cron_resume and cron_delete skills."
)
SEED_CRON_SCENARIO_TAGS = ("cron", "scheduler", "scenario")


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


async def migrate_cron_tools_to_native(service: InstructionService) -> None:
    """Replace the seeded HTTP cron tool records with the native-skills scenario.

    The first agent-managed cron used `external_call` against our own HTTP API;
    the native cron_* skills made those records obsolete — and harmful on
    surfaces without an HTTP listener (standalone Telegram runner). Idempotent:
    missing records are skipped, an up-to-date scenario is left untouched.
    """
    for title in SEED_CRON_HTTP_TOOL_TITLES:
        with suppress(InstructionNotFoundError):
            await service.delete(title, InstructionType.TOOL)
    try:
        scenario = await service.get_by_name(SEED_CRON_SCENARIO_TITLE, InstructionType.SKILL)
    except InstructionNotFoundError:
        scenario = None
    if scenario is None or scenario.content != SEED_CRON_SCENARIO_CONTENT:
        await service.save(
            InstructionType.SKILL,
            SEED_CRON_SCENARIO_TITLE,
            SEED_CRON_SCENARIO_CONTENT,
            SEED_CRON_SCENARIO_TAGS,
        )
