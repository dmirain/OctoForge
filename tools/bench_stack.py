"""Assemble and drive the real conversation stack for benchmarks."""

import asyncio
import time
from pathlib import Path
from typing import Any

from bench_runtime import BenchLLM, WaitTool
from bench_types import (
    CHANNEL,
    MAX_ITERATIONS,
    QUESTION,
    ROUTER_TIMEOUT,
    USER_ID,
    Marks,
    Script,
    Stack,
)
from octoforge_core import (
    AgentLoop,
    AgentLoopConfig,
    ConversationManager,
    DialogSubmission,
    Finished,
    TextDelta,
    ToolRegistry,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import StaticPromptProvider
from octoforge_core.agent.router import LLMRouter
from octoforge_core.agent.runner import ManagerStores, OwnershipConfig, RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.tasks.store import SqlAlchemyTaskStore


async def build_stack(scripts: list[Script], directory: Path) -> Stack:
    marks = Marks()
    engine = create_engine(f"sqlite+aiosqlite:///{directory / 'bench.db'}")
    await init_db(engine)
    sessions = create_session_factory(engine)
    llm = BenchLLM(scripts, marks)
    registry = ToolRegistry()
    registry.register(WaitTool(marks))
    prompts = StaticPromptProvider()
    manager = ConversationManager(
        config=RunnerConfig(
            loop=AgentLoop(llm, registry, AgentLoopConfig(MAX_ITERATIONS)),
            prompts=prompts,
            router=LLMRouter(llm, timeout_seconds=ROUTER_TIMEOUT, prompts=prompts),
            max_processes=5,
            compactor=NoopContextCompactor(),
        ),
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(sessions),
            messages=SqlAlchemyMessageRepository(sessions),
            tasks=SqlAlchemyTaskStore(sessions),
            exchanges=SqlAlchemyExchangeRepository(sessions),
            claims=SqlAlchemyClaimRepository(sessions),
            uow=UnitOfWork(sessions),
        ),
        ownership=OwnershipConfig(node_id="bench"),
    )
    return Stack(manager, marks, engine)


async def one_answer(
    stack: Stack,
    user_id: str = USER_ID,
    question: str = QUESTION,
) -> tuple[float, list[float]]:
    runner = await stack.manager.get_or_create_runner(user_id, CHANNEL)
    events = runner.subscribe()
    started = time.perf_counter()
    await runner.submit(DialogSubmission(question))
    return started, await drain(events, 1)


async def drain(events: asyncio.Queue[Any], finals: int) -> list[float]:
    arrivals: list[float] = []
    remaining = finals
    while remaining:
        payload = (await events.get()).payload
        if isinstance(payload, TextDelta):
            arrivals.append(time.perf_counter())
        elif isinstance(payload, Finished):
            remaining -= 1
    return arrivals
