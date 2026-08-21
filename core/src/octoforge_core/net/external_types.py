"""Configuration, ports and results of stored endpoint execution."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.instructions.api import InstructionService
from octoforge_core.net.external_messages import (
    MAX_BODY_CHARS,
)
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.params.api import UserParamStore
from octoforge_core.secrets.api import ResolvedSecret, SecretStore

if TYPE_CHECKING:
    from octoforge_core.net.collections.ingest import ResponseSpill


@dataclass(frozen=True, slots=True)
class ExternalCallAuth:
    base_url_prefix: str
    header_name: str
    header_value: str


@dataclass(frozen=True, slots=True)
class CallCredentials:
    auth_whitelist: tuple[ExternalCallAuth, ...] = ()
    secrets: SecretStore | None = None
    user_params: UserParamStore | None = None


@dataclass(frozen=True, slots=True)
class ExternalCallResult:
    status: int
    body: str


@dataclass(frozen=True, slots=True)
class CallOptions:
    """Collection and task-scope options of one endpoint call."""

    collect: bool = False
    max_pages: int | None = None
    into: str | None = None
    label: str = ""
    scope: str = ""


@dataclass(frozen=True, slots=True)
class ExternalCallContext:
    """Caller identity and optional response handling intent."""

    user_id: str | None = None
    options: CallOptions = field(default_factory=CallOptions)


@dataclass(frozen=True, slots=True)
class KindCallRequest:
    """A delegated non-HTTP endpoint invocation."""

    content: str
    params: dict[str, Any]
    user_id: str | None
    scope: str = ""


class KindCallDelegate(Protocol):
    async def execute(self, request: KindCallRequest) -> ExternalCallResult: ...


@dataclass(frozen=True, slots=True)
class ExternalCallServices:
    instructions: InstructionService
    http: httpx.AsyncClient
    guard: SsrfGuard


@dataclass(frozen=True, slots=True)
class ExternalCallConfig:
    credentials: CallCredentials = field(default_factory=CallCredentials)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    delegates: Mapping[str, KindCallDelegate] | None = None
    spill: "ResponseSpill | None" = None
    truncate_chars: int = MAX_BODY_CHARS


@dataclass(frozen=True, slots=True)
class PreparedHttpCall:
    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    secrets: tuple[ResolvedSecret, ...]


@dataclass(frozen=True, slots=True)
class ExternalPage:
    """One scrubbed HTTP page before spill or collection handling."""

    status: int
    body: str
    content_type: str
    wire_truncated: bool
    had_secrets: bool
