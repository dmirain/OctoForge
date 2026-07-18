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
SEED_CRON_MARKER_TOOL_TITLE = "cron_create_job"
SEED_CRON_TAGS = ("cron", "scheduler", "api")


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


async def seed_cron_tools_if_absent(service: InstructionService, base_url: str) -> None:
    """Write the cron API tool records pointing at our own base URL.

    Idempotent independently of `seed_if_empty`: the `cron_create_job` record
    is the marker, so a store seeded with only the weather baseline still gets
    the cron tools on the next start.
    """
    try:
        await service.get_by_name(SEED_CRON_MARKER_TOOL_TITLE, InstructionType.TOOL)
        return
    except InstructionNotFoundError:
        pass
    for seed in _cron_seed_instructions(base_url):
        await service.save(seed.kind, seed.title, seed.content, seed.tags)


def _cron_seed_instructions(base_url: str) -> tuple[SeedInstruction, ...]:
    jobs_url = f"{base_url}/api/cron/jobs"
    return (
        SeedInstruction(
            kind=InstructionType.TOOL,
            title=SEED_CRON_MARKER_TOOL_TITLE,
            content=json.dumps(
                {
                    "method": "POST",
                    "url_template": (
                        f"{jobs_url}?title={{title}}&schedule={{schedule}}"
                        "&prompt={prompt}&timezone={timezone}"
                    ),
                    "params_schema": {
                        "title": {
                            "type": "string",
                            "required": True,
                            "description": "Short job name, e.g. 'morning report'",
                        },
                        "schedule": {
                            "type": "string",
                            "required": True,
                            "description": "Cron expression, e.g. '0 9 * * *' for daily at 09:00",
                        },
                        "prompt": {
                            "type": "string",
                            "required": True,
                            "description": "Instruction the agent receives on every firing",
                        },
                        "timezone": {
                            "type": "string",
                            "required": True,
                            "description": (
                                'IANA timezone, e.g. "Europe/Moscow"; use "UTC" if unknown'
                            ),
                        },
                    },
                    "auth": "none",
                }
            ),
            tags=SEED_CRON_TAGS,
        ),
        SeedInstruction(
            kind=InstructionType.TOOL,
            title="cron_list_jobs",
            content=json.dumps(
                {
                    "method": "GET",
                    "url_template": jobs_url,
                    "params_schema": {},
                    "auth": "none",
                }
            ),
            tags=SEED_CRON_TAGS,
        ),
        SeedInstruction(
            kind=InstructionType.TOOL,
            title="cron_delete_job",
            content=json.dumps(
                {
                    "method": "DELETE",
                    "url_template": f"{jobs_url}/{{job_id}}",
                    "params_schema": {"job_id": {"type": "string", "required": True}},
                    "auth": "none",
                }
            ),
            tags=SEED_CRON_TAGS,
        ),
        SeedInstruction(
            kind=InstructionType.TOOL,
            title="cron_pause_job",
            content=json.dumps(
                {
                    "method": "POST",
                    "url_template": f"{jobs_url}/{{job_id}}/pause",
                    "params_schema": {"job_id": {"type": "string", "required": True}},
                    "auth": "none",
                }
            ),
            tags=SEED_CRON_TAGS,
        ),
        SeedInstruction(
            kind=InstructionType.TOOL,
            title="cron_resume_job",
            content=json.dumps(
                {
                    "method": "POST",
                    "url_template": f"{jobs_url}/{{job_id}}/resume",
                    "params_schema": {"job_id": {"type": "string", "required": True}},
                    "auth": "none",
                }
            ),
            tags=SEED_CRON_TAGS,
        ),
        SeedInstruction(
            kind=InstructionType.SKILL,
            title="schedule_a_recurring_report",
            content=(
                "Scenario: schedule a recurring report or reminder.\n"
                "1. Find the cron tools via instructions_search (query 'cron schedule').\n"
                "2. Compose the cron expression for the requested cadence "
                "(e.g. '0 9 * * *' for every day at 09:00) and the user's IANA timezone "
                "(ask when unknown; use 'UTC' when unclear).\n"
                "3. Call external_call with name 'cron_create_job' and params "
                '{"title": ..., "schedule": ..., "prompt": ..., "timezone": ...}; '
                "the prompt is the instruction you will receive on every firing.\n"
                "4. Confirm the created job to the user: title, schedule, timezone "
                "and the next_fire_at from the response. Manage jobs later with "
                "cron_list_jobs, cron_pause_job, cron_resume_job and cron_delete_job."
            ),
            tags=("cron", "scenario", "api"),
        ),
    )
