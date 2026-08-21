"""Knowledge stores and the declarative system-skill registry."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from octoforge_core import (
    build_dataset_service,
    build_dataset_store,
    build_instruction_service,
    build_instruction_store,
    build_summary_store,
)
from octoforge_core.composition import InstructionServiceOptions
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.datasets.api import DatasetService
from octoforge_core.db.search_extensions import VECTOR
from octoforge_core.errors import LLMResponseError
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.registry import (
    CORE_SYSTEM_SKILLS,
    SystemSkill,
    sync_system_registry,
)
from octoforge_core.llm.errors import LLMError
from octoforge_server.config import Settings
from octoforge_server.skill_overlay import apply_overlay, load_overlay
from octoforge_server.system_skills import WEB_SYSTEM_SKILLS
from sqlalchemy.exc import SQLAlchemyError

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import LanguageServices

logger = logging.getLogger("octoforge_deploy.main")


@dataclass(frozen=True, slots=True)
class KnowledgeServices:
    instructions: InstructionService
    datasets: DatasetService
    summaries: SqlAlchemySummaryStore


async def build_knowledge_services(
    settings: Settings,
    database: DatabaseRuntime,
    language: LanguageServices,
) -> KnowledgeServices:
    instructions = build_instruction_service(
        build_instruction_store(
            database.sessions,
            vector_search=VECTOR in database.extensions,
        ),
        language.embedder,
        InstructionServiceOptions(
            reranker=language.reranker,
            rerank_candidates=settings.reranker_candidates,
            embedding_model=settings.embedding_model,
        ),
    )
    datasets = build_dataset_service(
        build_dataset_store(database.sessions, lexical_search=database.lexical),
        language.embedder,
        limits=database.stores.limits,
    )
    summaries = build_summary_store(database.sessions, lexical_search=database.lexical)
    await _sync_system_skills(instructions, settings)
    return KnowledgeServices(instructions, datasets, summaries)


async def _sync_system_skills(instructions: InstructionService, settings: Settings) -> None:
    if not settings.embeddings_configured():
        return
    try:
        await sync_system_registry(instructions, _system_registry(settings))
        resynced = await instructions.resync_embeddings()
        if resynced:
            logger.info("re-embedded %d record(s) whose vector was missing or stale", resynced)
    except (LLMError, LLMResponseError, SQLAlchemyError):
        logger.warning("System skill registry sync failed; starting without it", exc_info=True)


def _system_registry(settings: Settings) -> tuple[SystemSkill, ...]:
    registry = CORE_SYSTEM_SKILLS + WEB_SYSTEM_SKILLS
    path = settings.to_skills_overlay_path()
    if path is None:
        return registry
    patches = load_overlay(path)
    if not patches:
        return registry
    logger.info("system skills overlay applied: %d patch(es) from %s", len(patches), path)
    return apply_overlay(registry, patches)
