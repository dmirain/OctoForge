"""Declarative system registry: the installer-owned instructions.

A system registry is a flat list of records (system skills and their
endpoints) assembled in the composition root. `sync_system_registry` replaces
the system-owned slice of the store with it at startup: entries are upserted
as system records (adopting same-named legacy user records), system records
missing from the registry are deleted, and user records (system=False) are
never touched. Core ships the default registry below — one scenario per
module, moving the tool-usage methodology out of the system prompt into
searchable scenarios; the installer adds application packs on top.
"""

from dataclasses import dataclass

from octoforge_core.instructions.api import InstructionService, InstructionType


@dataclass(frozen=True, slots=True)
class SystemSkill:
    """One registry entry: a record to keep present as a system instruction."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]


CRON_JOBS_CONTENT = """\
Scenario: scheduled and recurring jobs (reminders, periodic reports).
Anything tied to a future time or a schedule belongs to cron — never to task_spawn.
1. Work out the cadence. Compose the cron expression yourself ('0 9 * * *' = daily
   09:00). Ask the user's IANA timezone when unknown; use 'UTC' when unclear.
2. One-time reminder: one_shot=true and a dated expression (minute hour day-of-month
   month *) with the nearest future occurrence, e.g. '30 15 21 7 *' for Jul 21 15:30.
   One-shot jobs delete themselves after firing.
3. Call cron_create with title, schedule, prompt and timezone. The prompt is the
   instruction you receive on every firing — make it self-contained.
4. Confirm to the user: title, schedule, timezone, next fire time.
Managing jobs: cron_list to inspect, cron_pause/cron_resume for temporary disabling.
Before cron_delete: show the job from cron_list and confirm the exact job with the user.
Creating a duplicate returns 'already exists' — do not retry, show the existing job."""

BACKGROUND_TASKS_CONTENT = """\
Scenario: run real work in the background (result needed once, when ready).
1. Call task_spawn with a self-contained prompt describing the work.
2. Confirm to the user that the task started, then continue the conversation —
   do not wait for the result.
3. When a system message reports the task finished, briefly relay the result.
Use task_list to check status of this conversation's background tasks.
Not for reminders or anything scheduled — that is cron_create. If spawning is
refused because of the process limit, tell the user instead of retrying in a loop."""

USER_MEMORY_CONTENT = """\
Scenario: durable user facts and preferences (name, city, diet, goals and the like).
1. Save facts with memory_store, scope user. Use scope global only for facts shared
   by everyone, and with care.
2. Call memory_search before personal recommendations or when the answer may depend
   on what the user told you earlier.
3. Do not duplicate what lives in instructions (shared knowledge) or datasets
   (structured records). Memory is per-user and shared across the user's surfaces.
Delete with memory_delete only on the user's explicit request."""

USER_DATASETS_CONTENT = """\
Scenario: remember and track structured data for the user (food, weight, habits...).
1. Find the dataset via skills_search; if none fits, create it implicitly with
   data_put by declaring a JSON schema for the record.
2. Write records with data_put; read and build reports with data_query (equality
   filters, date ranges, limit).
3. Delete data with data_forget — after confirming with the user what will be deleted.
Datasets are private to the user: never mix one user's data into another's answers."""

HISTORY_LOOKUP_CONTENT = """\
Scenario: look up something discussed earlier in this conversation.
Your context holds compressed summaries of earlier topics; only the recent tail is
verbatim. If the user refers to something not covered by the summaries or the tail,
call history_search with the distinctive phrase instead of asking the user to repeat
it. Narrow with topic/date filters when the first search is too broad."""

WEB_LOOKUP_CONTENT = """\
Scenario: look up current events or facts you do not know.
Call web_search with a focused query; answer from the results and cite the source
links. If the results are thin or contradictory, say so instead of guessing."""

EXTERNAL_HTTP_CONTENT = """\
Scenario: call an external API.
1. Prefer a discovered endpoint: call external_call with the endpoint name and its
   declared params instead of hand-crafting requests.
2. Use http_request only for one-off calls not covered by any endpoint.
Outbound calls pass a security guard: public hosts only, no redirects. If a call is
refused, report the refusal honestly instead of retrying variations."""

SKILL_AUTHORING_CONTENT = """\
Scenario: find and author skill scenarios.
1. For every intent in the user's message call skills_search with a single
   free-text query: phrase it as the normalized intent (remind, schedule,
   report, track, lookup, save, call-api) plus the entity type (reminder,
   recurring-report, user-data, weather, history, web-fact), e.g.
   'remind reminder'; add free text only when it narrows the search. Do not
   improvise tool usage before searching — the scenario says how to use the
   tools correctly.
2. After completing a novel multi-step task, save the working scenario with
   instruction_save (type skill): clear steps, naming every tool the scenario uses.
   Save durable facts as type knowledge.
3. Search before saving: update the existing scenario instead of creating a duplicate."""

CORE_SYSTEM_SKILLS: tuple[SystemSkill, ...] = (
    SystemSkill(
        kind=InstructionType.SKILL,
        title="cron_jobs",
        content=CRON_JOBS_CONTENT,
        tags=("cron", "scheduler", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="background_tasks",
        content=BACKGROUND_TASKS_CONTENT,
        tags=("tasks", "background", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="user_memory",
        content=USER_MEMORY_CONTENT,
        tags=("memory", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="user_datasets",
        content=USER_DATASETS_CONTENT,
        tags=("datasets", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="history_lookup",
        content=HISTORY_LOOKUP_CONTENT,
        tags=("history", "search", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="web_lookup",
        content=WEB_LOOKUP_CONTENT,
        tags=("web", "search", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="external_http",
        content=EXTERNAL_HTTP_CONTENT,
        tags=("http", "endpoint", "scenario"),
    ),
    SystemSkill(
        kind=InstructionType.SKILL,
        title="skill_authoring",
        content=SKILL_AUTHORING_CONTENT,
        tags=("skills", "authoring", "scenario"),
    ),
)


async def sync_system_registry(
    service: InstructionService,
    entries: tuple[SystemSkill, ...],
) -> None:
    """Replace the system-owned slice of the store with the declarative registry.

    Entries are upserted as system records (adopting same-named legacy user
    records); system records missing from `entries` are deleted. User records
    (system=False) are never touched. Goes through the facade, so it keeps
    working when the module is extracted behind an HTTP boundary.
    """
    for entry in entries:
        await service.save_system(entry.kind, entry.title, entry.content, entry.tags)
    keep = {(entry.kind.value, entry.title) for entry in entries}
    for record in await service.list_system():
        if (record.type.value, record.title) not in keep:
            await service.delete_system(record.title, record.type)
