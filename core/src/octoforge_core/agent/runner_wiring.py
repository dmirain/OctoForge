"""Internal service graph of one dialog actor."""

from typing import TYPE_CHECKING

from .runner_actor import ActorInbox
from .runner_answer import AnswerRuns
from .runner_broadcast import EventBroadcaster
from .runner_exchanges import ExchangeCoordinator
from .runner_finalize import ProcessFinalizer
from .runner_jobs import BackgroundJobs
from .runner_lifecycle import RunnerLifecycle
from .runner_material import MaterialCollector
from .runner_material_promotion import MaterialPromoter
from .runner_narrative import NarrativeContext
from .runner_outbox import DeliveryOutbox
from .runner_processes import ProcessRegistry
from .runner_pump import ProcessPump
from .runner_route_application import RouteApplier
from .runner_routing import ExchangeRouter
from .runner_settlement import ProcessSettlement
from .runner_stream import ProcessStream
from .runner_tariffs import TariffNotices
from .runner_task_recovery import TaskRecovery
from .runner_usage import RunnerUsage
from .runner_vision import DialogVision

if TYPE_CHECKING:
    from .runner_facade import ConversationRunner

RunnerServices = tuple[
    ActorInbox,
    RunnerLifecycle,
    EventBroadcaster,
    DeliveryOutbox,
    NarrativeContext,
    DialogVision,
    RunnerUsage,
    MaterialCollector,
    MaterialPromoter,
    ExchangeRouter,
    RouteApplier,
    ExchangeCoordinator,
    ProcessSettlement,
    ProcessRegistry,
    BackgroundJobs,
    TariffNotices,
    TaskRecovery,
    AnswerRuns,
    ProcessStream,
    ProcessFinalizer,
    ProcessPump,
]


def build_runner_services(runner: "ConversationRunner") -> RunnerServices:
    return (
        ActorInbox(runner),
        RunnerLifecycle(runner),
        EventBroadcaster(runner),
        DeliveryOutbox(runner),
        NarrativeContext(runner),
        DialogVision(runner),
        RunnerUsage(runner),
        MaterialCollector(runner),
        MaterialPromoter(runner),
        ExchangeRouter(runner),
        RouteApplier(runner),
        ExchangeCoordinator(runner),
        ProcessSettlement(runner),
        ProcessRegistry(runner),
        BackgroundJobs(runner),
        TariffNotices(runner),
        TaskRecovery(runner),
        AnswerRuns(runner),
        ProcessStream(runner),
        ProcessFinalizer(runner),
        ProcessPump(runner),
    )
