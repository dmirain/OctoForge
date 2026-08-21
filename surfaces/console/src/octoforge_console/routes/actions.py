"""Request-scoped service groups for console reads and mutations."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from .common import DialogCascadeDep
from .deps import (
    CronStoreDep,
    DialogsDep,
    IdentityStoreDep,
    InstructionsDep,
    KnownFeaturesDep,
    ManagerDep,
    NotifierDep,
    OperatorDep,
    ReadModelDep,
    TariffStoreDep,
    UserParamsDep,
)


@dataclass(frozen=True, slots=True)
class ListingServices:
    read_model: ReadModelDep
    identities: IdentityStoreDep


@dataclass(frozen=True, slots=True)
class DialogRuntime:
    dialogs: DialogsDep
    manager: ManagerDep
    stores: DialogCascadeDep


@dataclass(frozen=True, slots=True)
class DialogAction:
    operator: OperatorDep
    runtime: Annotated[DialogRuntime, Depends()]


@dataclass(frozen=True, slots=True)
class CronAction:
    operator: OperatorDep
    store: CronStoreDep


@dataclass(frozen=True, slots=True)
class InstructionAction:
    operator: OperatorDep
    service: InstructionsDep


@dataclass(frozen=True, slots=True)
class ParamAction:
    operator: OperatorDep
    store: UserParamsDep


@dataclass(frozen=True, slots=True)
class TariffCatalog:
    store: TariffStoreDep
    known_features: KnownFeaturesDep


@dataclass(frozen=True, slots=True)
class TariffAction:
    operator: OperatorDep
    catalog: Annotated[TariffCatalog, Depends()]


@dataclass(frozen=True, slots=True)
class IdentityAction:
    operator: OperatorDep
    identities: IdentityStoreDep


@dataclass(frozen=True, slots=True)
class UserStatusAction:
    identity: Annotated[IdentityAction, Depends()]
    cron_store: CronStoreDep
    notifier: NotifierDep


ListingServicesDep = Annotated[ListingServices, Depends()]
DialogActionDep = Annotated[DialogAction, Depends()]
CronActionDep = Annotated[CronAction, Depends()]
InstructionActionDep = Annotated[InstructionAction, Depends()]
ParamActionDep = Annotated[ParamAction, Depends()]
TariffActionDep = Annotated[TariffAction, Depends()]
UserStatusActionDep = Annotated[UserStatusAction, Depends()]
