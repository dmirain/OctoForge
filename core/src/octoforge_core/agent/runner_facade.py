"""Small public facade for one per-dialog actor."""

from octoforge_core.tasks.api import Task
from octoforge_core.tools.base import TaskDeleteOutcome

from .runner_api import DialogSubmission
from .runner_broadcast import EventBroadcaster
from .runner_commands import ProcessTerminated
from .runner_process import Process
from .runner_state import RunnerParts, RunnerRuntime
from .runner_tool_ports import DialogTaskDeleter, DialogTaskSpawner
from .runner_transport import RunnerTransport
from .runner_wiring import build_runner_services


class ConversationRunner(RunnerTransport):
    """Owns a dialog's narrative, obligations, processes and deliveries."""

    def __init__(self, parts: RunnerParts) -> None:
        self._seed, self._config, self._stores = parts.seed, parts.config, parts.stores
        self._runtime = RunnerRuntime(parts.seed.history)
        (
            self._actor,
            self._lifecycle,
            self._broadcaster,
            self._outbox,
            self._context,
            self._vision,
            self._usage,
            self._material,
            self._material_promotion,
            self._routing,
            self._route_applier,
            self._exchanges,
            self._settlement,
            self._process_registry,
            self._jobs,
            self._tariffs,
            self._recovery,
            self._answer,
            self._stream,
            self._finalizer,
            self._pump,
        ) = build_runner_services(self)
        self._processes = self._runtime.processes
        self._pending_deliveries = self._runtime.pending_deliveries
        self._inbox, self._narrative = self._runtime.inbox, self._runtime.narrative
        self._runtime.spawner, self._runtime.deleter = (
            DialogTaskSpawner(self),
            DialogTaskDeleter(self),
        )

    def start(self) -> None:
        self._lifecycle.start()

    async def stop(self) -> None:
        await self._lifecycle.stop()

    async def submit(self, submission: DialogSubmission) -> None:
        await self._actor.submit(submission)

    async def cancel(self) -> None:
        self._answer.cancel_live()
        await self._answer.cancel_parked()

    async def spawn_task(self, title: str, prompt: str) -> str:
        return await self._jobs.spawn(title, prompt)

    async def wake(self, title: str, prompt: str, cron_job_id: str) -> bool:
        return await self._jobs.wake(title, prompt, cron_job_id)

    async def delete_task(self, task_id: str) -> TaskDeleteOutcome:
        return await self._jobs.delete(task_id)

    async def restart_task(self, task: Task) -> None:
        await self._jobs.restart(task)

    async def stand_down(self) -> None:
        await self._lifecycle.stand_down()

    async def promote_collected(self, exchange_id: str) -> None:
        await self._material.nominate(exchange_id)

    async def ask_user(self, process_id: str, question: str) -> bool:
        return await self._answer.ask_user(process_id, question)

    async def look_at_image(self, question: str) -> str:
        return await self._vision.look(question)

    async def resume_stranded(self) -> None:
        await self._exchanges.sweep_unowned_open()

    def request_result_delivery(self, task_id: str) -> None:
        self._runtime.inbox.put_nowait(ProcessTerminated(task_id))

    async def _settle_exchange(self, command: ProcessTerminated) -> None:
        await self._settlement.settle_exchange(command)

    def _live_process_for(self, exchange_id: str | None) -> Process | None:
        return self._exchanges.live_process_for(exchange_id)

    def _trim_narrative(self, drop: int) -> None:
        self._context.trim(drop)

    _evict_and_put = staticmethod(EventBroadcaster._evict_and_put)
