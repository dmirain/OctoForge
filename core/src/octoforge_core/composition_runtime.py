"""Conversation, scheduler and collection-loop composition."""

from octoforge_core.agent.collecting import (
    CollectingSources,
    CollectingSweepConfig,
    CollectingSweeper,
    CollectionPromoter,
)
from octoforge_core.agent.runner import (
    ConversationManager,
    ManagerStores,
    OwnershipConfig,
    RunnerConfig,
)
from octoforge_core.composition_types import RunnerOptions, RunnerServices
from octoforge_core.cron.api import CronStore, CronWaker, Scheduler
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig


def build_runner_config(services: RunnerServices, options: RunnerOptions) -> RunnerConfig:
    """Build the behavior config shared by one conversation manager."""
    return RunnerConfig(
        loop=services.loop,
        prompts=services.prompts,
        router=services.router,
        max_processes=options.max_processes,
        material_quiet_seconds=options.material_quiet_seconds,
        compactor=services.compactor,
        task_outcome_listener=options.task_outcome_listener,
        vision=options.vision,
        image_resolver=options.image_resolver,
        limits=options.limits,
        response_memory=options.response_memory,
    )


def build_conversation_manager(
    config: RunnerConfig,
    stores: ManagerStores,
    ownership: OwnershipConfig,
) -> ConversationManager:
    """Build the manager owning one runner per dialog."""
    return ConversationManager(config=config, stores=stores, ownership=ownership)


def build_cron_scheduler(
    store: CronStore,
    waker: CronWaker,
    config: CronSchedulerConfig,
) -> Scheduler:
    """Build the cron scheduler; starting it remains the caller's job."""
    return CronScheduler(store=store, waker=waker, config=config)


def build_collecting_sweeper(
    sources: CollectingSources,
    promoter: CollectionPromoter,
    config: CollectingSweepConfig,
) -> CollectingSweeper:
    """Build the collection sweep; starting it remains the caller's job."""
    return CollectingSweeper(sources=sources, promoter=promoter, config=config)
