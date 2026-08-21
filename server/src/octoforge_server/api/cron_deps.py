"""Request-scoped identity and services for cron endpoints."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from octoforge_core.cron.api import CronStore
from octoforge_core.tariffs.api import LimitGate

from octoforge_server.deps import get_channel, get_cron_store, get_limit_gate, get_user_id


@dataclass(frozen=True, slots=True)
class CronActor:
    user_id: Annotated[str, Depends(get_user_id)]
    channel: Annotated[str, Depends(get_channel)]


@dataclass(frozen=True, slots=True)
class CronServices:
    store: Annotated[CronStore, Depends(get_cron_store)]
    gate: Annotated[LimitGate, Depends(get_limit_gate)]


CronActorDep = Annotated[CronActor, Depends()]
CronServicesDep = Annotated[CronServices, Depends()]
