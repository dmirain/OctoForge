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


DEFERRED_WORK_CONTENT = """\
Scenario: deferred work — background tasks and scheduled jobs (reminders, reports).
1. Work now, result later: call task_create without a schedule — the task runs in
   the background; confirm it started and continue the conversation, do not wait.
   When a system message reports it finished, briefly relay the result.
2. Future or recurring: call task_create with a schedule (compose the cron
   expression yourself; '0 9 * * *' = daily 09:00) and timezone (ask the user
   when unknown, else 'UTC'). One-time reminder: one_shot=true and a dated
   expression (minute hour day-of-month month *), e.g. '30 15 21 7 *' for
   Jul 21 15:30; one-shot jobs delete themselves after firing.
3. The prompt is the instruction the task receives on start / every firing —
   make it self-contained; this conversation's context may be gone by then.
Managing: task_list shows running tasks and scheduled jobs (finished tasks
disappear from it — their results stay in the conversation); task_delete removes
either (stopping a running task first); cron_pause/cron_resume temporarily
disable a scheduled job. Before deleting a scheduled job, show it from task_list
and confirm with the user. A duplicate scheduled job returns 'already exists' —
show the existing one instead of retrying. If spawning is refused because of the
process limit, tell the user instead of retrying in a loop."""

USER_MEMORY_CONTENT = """\
Scenario: durable user facts and preferences (name, city, diet, goals and the like).
1. Save facts with memory_store. Memory is private to this user; a fact useful to
   everyone is saved as a knowledge record via instruction_save instead (an admin
   publishes it later).
2. Memories come back through recall, ranked together with skills and
   knowledge. Before personal recommendations, or when the answer may depend on
   what the user told you earlier, add a query about the user (e.g. 'user
   preferences diet'); type=memory narrows the search to memories only. When the
   user asks what you remember about them, search type=memory with broad queries
   and present the hits honestly.
3. Do not duplicate what lives in instructions (shared knowledge) or datasets
   (structured records). Memory is per-user and shared across the user's surfaces.
Delete with memory_delete only on the user's explicit request."""

USER_DATASETS_CONTENT = """\
Scenario: remember and track structured data for the user (food, weight, habits...).
1. Find the dataset via recall; if none fits, create it implicitly with
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
1. Skills name the endpoints they use. Before the FIRST call of an endpoint in this
   process, resolve its contract with endpoint_get(name) — it returns the method,
   URL template and declared params; the contract stays in your context for repeat
   calls. Several endpoints resolve in one turn (parallel endpoint_get calls).
2. Execute with external_call(name, params), passing exactly the declared params.
   Never call blindly: guessed params fail, and the error will just hand you the
   contract you should have fetched first.
3. No skill names an endpoint? Check whether the integration exists at all:
   recall(type=endpoint, query='...'). Found and used successfully — save a skill
   naming it (instruction_save), so the next run skips discovery.
4. Use http_request only for one-off calls not covered by any endpoint.
Outbound calls pass a security guard: public hosts only, no redirects. If a call is
refused, report the refusal honestly instead of retrying variations."""

ABOUT_OCTOFORGE_CONTENT = """\
About this system: you are OctoForge, a multi-user LLM agent. Your capabilities are
data, not code: skill scenarios say how tasks are done here, knowledge records hold
shared facts, endpoint records describe external APIs you can call, and each user has
a private memory and private datasets. All of it lives in a searchable store that
admins extend without redeploying you. You serve several surfaces (web chat, Telegram);
each user's dialogs, memory and datasets are isolated from other users.
Installation-specific facts — who operates this deployment, who its author is, what
community it serves — are separate knowledge records maintained by the admins: search
for them, and when no record answers the question, say you do not know rather than
guessing."""

SKILL_AUTHORING_CONTENT = """\
Scenario: find and author skill scenarios.
1. For every intent in the user's message call recall with a single
   free-text query: phrase it as the normalized intent (remind, schedule,
   report, track, lookup, save, call-api) plus the entity type (reminder,
   recurring-report, user-data, weather, history, web-fact), e.g.
   'remind reminder'; add free text only when it narrows the search. Do not
   improvise tool usage before searching — the scenario says how to use the
   tools correctly. A found scenario is binding: follow its steps as written
   rather than solving the task your own way, and improvise only after a search
   returned nothing usable.
2. After completing a novel multi-step task, save the working scenario with
   instruction_save (type skill): clear steps, naming every tool the scenario uses.
   Save durable facts useful to everyone as type knowledge.
3. Search before saving: update the existing scenario instead of creating a duplicate."""

CORE_SYSTEM_SKILLS: tuple[SystemSkill, ...] = (
    SystemSkill(
        kind=InstructionType.SKILL,
        title="deferred_work",
        content=DEFERRED_WORK_CONTENT,
        tags=("cron", "scheduler", "tasks", "background", "scenario"),
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
    SystemSkill(
        kind=InstructionType.KNOWLEDGE,
        title="about_octoforge",
        content=ABOUT_OCTOFORGE_CONTENT,
        tags=("identity", "system", "author", "capabilities"),
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
