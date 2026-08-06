"""Public boundary of the tariffs module.

A tariff is an operator-managed plan: a set of enabled features plus numeric
caps. A user is bound to at most one tariff; no binding means no restrictions,
and zero tariff rows leave the whole mechanism inert — configuration is data,
"empty config = feature off" applied to limits themselves.

Consumption is an insert-only ledger of usage events, not a running counter:
every metered action appends one row carrying its kind, its origin and the ids
of the entities it belongs to. Any window (a UTC day for limits, a month for a
report) is a sum over the ledger, and concurrent writers on different nodes
never contend.

Tariff codes share the secrets/params grammar (`^[a-z0-9_]{1,64}$`) so the
operator never has to remember two naming rules.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_TITLE_CHARS = 256
# What a tool answers when the caller's plan does not include the feature.
FEATURE_REFUSAL_TEMPLATE = "not available on the current plan: {feature}"


class TariffNotFoundError(Exception):
    """Raised when no tariff matches the requested code."""


class TariffInUseError(Exception):
    """Raised when deleting a tariff that users are still assigned to."""


class InvalidTariffError(Exception):
    """Raised when a tariff being stored is malformed (code, title or limit)."""


class FeatureCode(StrEnum):
    """The switchable capabilities shipped with the core.

    A feature is a plain string everywhere in the data path (`Tariff.features`,
    `ToolContext.enabled_features`), so an installer adds its own codes without
    touching this module: declare the string next to the tool, gate the tool
    with `feature_enabled`, and include the code in the `known_features` set
    the composition root hands the console (see `CORE_FEATURES`). This enum is
    the core's own vocabulary — its members are strings, so they pass anywhere
    a feature code is expected. A user without a tariff has every feature.
    """

    SKILL_CREATE = "skill_create"
    VOICE_TRANSCRIPTION = "voice_transcription"
    WEB_SEARCH = "web_search"
    MCP_ADD = "mcp_add"
    HTTP_ENDPOINTS = "http_endpoints"
    VISION = "vision"


#: Feature codes shipped with the core; a composition root passes
#: `CORE_FEATURES | {its own codes}` as the console's vocabulary.
CORE_FEATURES: frozenset[str] = frozenset(code.value for code in FeatureCode)


class UsageKind(StrEnum):
    """What a usage event measures."""

    LLM_ANSWER = "llm_answer"  # one finished run: tokens of every iteration, quantity=1
    LLM_ROUTING = "llm_routing"
    LLM_COMPACTION = "llm_compaction"
    VOICE_TRANSCRIPTION = "voice_transcription"  # quantity = audio seconds
    VISION = "vision"  # quantity = images inspected
    USER_MESSAGE = "user_message"  # quantity = 1


class UsageOrigin(StrEnum):
    """What triggered the spend: a live dialog, a cron wake or a spawned task."""

    INTERACTIVE = "interactive"
    CRON = "cron"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class TariffLimits:
    """Numeric caps of one plan; a `None` means unlimited in that dimension."""

    daily_tokens: int | None = None
    daily_user_messages: int | None = None
    daily_assistant_messages: int | None = None
    max_cron_jobs: int | None = None
    max_datasets: int | None = None

    def normalized(self) -> "TariffLimits":
        """Validate every cap; raise `InvalidTariffError` on a negative one."""
        return TariffLimits(
            daily_tokens=normalize_limit(self.daily_tokens, "daily_tokens"),
            daily_user_messages=normalize_limit(self.daily_user_messages, "daily_user_messages"),
            daily_assistant_messages=normalize_limit(
                self.daily_assistant_messages, "daily_assistant_messages"
            ),
            max_cron_jobs=normalize_limit(self.max_cron_jobs, "max_cron_jobs"),
            max_datasets=normalize_limit(self.max_datasets, "max_datasets"),
        )


@dataclass(frozen=True, slots=True)
class Tariff:
    """One plan: a feature set (plain codes, extensible) plus its numeric caps."""

    id: str
    code: str
    title: str
    features: frozenset[str]
    limits: TariffLimits
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One metered action; link ids are whatever exists at the call site.

    Routing happens before any task and a user message belongs to no task at
    all, so `task_id` (and the other links) are optional — where a task does
    exist, its id leads to every related entity.
    """

    user_id: str
    kind: UsageKind
    origin: UsageOrigin
    prompt_tokens: int = 0
    completion_tokens: int = 0
    quantity: int = 0
    dialog_id: str | None = None
    exchange_id: str | None = None
    task_id: str | None = None
    created_at: datetime | None = None  # stamped by the store on record()


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Aggregated consumption of one user over a window."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    user_messages: int = 0
    assistant_messages: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LimitVerdict:
    """Outcome of a limit check; `reason` names the exhausted dimension."""

    allowed: bool
    reason: str | None = None
    used: int = 0
    limit: int = 0

    @classmethod
    def ok(cls) -> "LimitVerdict":
        return cls(allowed=True)


def feature_enabled(features: frozenset[str] | None, feature: str) -> bool:
    """Whether a feature set (as carried by `ToolContext`) grants the feature.

    `None` means "no gating at all" — the shape a user without a tariff gets.
    Takes any string so installer-defined codes work; `FeatureCode` members
    are strings and pass unchanged.
    """
    return features is None or feature in features


def feature_refusal(feature: str) -> str:
    """The uniform tool answer for a feature the plan does not include."""
    return FEATURE_REFUSAL_TEMPLATE.format(feature=feature)


def normalize_feature(raw: str) -> str:
    """Validate and normalize a feature code (same grammar as tariff codes)."""
    feature = raw.strip().lower()
    if not CODE_PATTERN.match(feature):
        raise InvalidTariffError(
            "feature code must be 1-64 characters of [a-z0-9_], e.g. 'web_search'"
        )
    return feature


def normalize_code(raw: str) -> str:
    """Validate and normalize a tariff code (snake_case, ≤64 chars)."""
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidTariffError("tariff code must be 1-64 characters of [a-z0-9_], e.g. 'basic'")
    return code


def normalize_title(raw: str) -> str:
    """Validate a tariff title: non-empty, bounded."""
    title = raw.strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise InvalidTariffError(f"tariff title must be 1..{MAX_TITLE_CHARS} characters")
    return title


def normalize_limit(raw: int | None, name: str) -> int | None:
    """Validate a numeric limit: None (unlimited) or a non-negative int."""
    if raw is None:
        return None
    if raw < 0:
        raise InvalidTariffError(f"{name} must be non-negative or unlimited (null)")
    return raw


class UsageRecorder(Protocol):
    """The metering side alone — for components that only ever spend."""

    async def record(self, event: UsageEvent) -> None:
        """Append a usage event; must never raise into the caller."""
        ...


class LimitGate(UsageRecorder, Protocol):
    """What consumers of the tariffs module see: limit checks plus metering.

    Implemented by `tariffs.service.LimitService`; consumers depend on this
    port so the concrete service stays behind the module boundary.
    """

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        """The user's feature codes as plain strings; `None` = everything on."""
        ...

    async def allows(self, user_id: str, feature: str) -> bool:
        """Whether the user's tariff grants the feature (any code, core or custom)."""
        ...

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        """May an LLM run start today (user messages, answers, tokens)?

        The single budget choke point: every run — an answer, a cron wake, a
        spawned background task — asks this and nothing else does. A refused
        message is still persisted; only the run is withheld.
        """
        ...

    async def max_cron_jobs(self, user_id: str) -> int | None:
        """The user's cron-job cap; `None` = unlimited."""
        ...

    async def max_datasets(self, user_id: str) -> int | None:
        """The user's dataset cap; `None` = unlimited."""
        ...


class TariffStore(Protocol):
    """Port of the tariff catalog and user assignments."""

    async def put(
        self,
        code: str,
        title: str,
        features: frozenset[str],
        limits: TariffLimits | None = None,
    ) -> Tariff:
        """Create or replace the tariff under `code`; feature codes are
        grammar-checked here, vocabulary-checked at the operator boundary."""
        ...

    async def list(self) -> list[Tariff]:
        """Return every tariff ordered by code."""
        ...

    async def delete(self, code: str) -> None:
        """Delete a tariff; raise `TariffInUseError` while users are assigned."""
        ...

    async def assign(self, user_id: str, code: str | None) -> None:
        """Bind the user to the tariff, or unbind with `None` (= unlimited)."""
        ...

    async def tariff_for_user(self, user_id: str) -> Tariff | None:
        """Return the user's tariff; `None` means no restrictions."""
        ...

    async def assignments(self) -> dict[str, str]:
        """Return a user_id → tariff code mapping of every assignment."""
        ...


class UsageMeter(Protocol):
    """Port of the usage ledger. `record` opens its own short transaction —
    call it after the surrounding unit of work commits, never inside one."""

    async def record(self, event: UsageEvent) -> None:
        """Append one usage event; `created_at` is stamped here."""
        ...

    async def totals_since(self, user_id: str, since: datetime) -> UsageTotals:
        """Aggregate the user's events at or after `since`."""
        ...
