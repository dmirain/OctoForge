"""Tariff catalog values, feature codes and errors."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TariffNotFoundError(Exception):
    """Raised when no tariff matches the requested code."""


class TariffInUseError(Exception):
    """Raised when deleting a tariff still assigned to users."""


class InvalidTariffError(Exception):
    """Raised when tariff data is malformed."""


def normalize_limit(raw: int | None, name: str) -> int | None:
    if raw is not None and raw < 0:
        raise InvalidTariffError(f"{name} must be non-negative or unlimited (null)")
    return raw


class FeatureCode(StrEnum):
    SKILL_CREATE = "skill_create"
    VOICE_TRANSCRIPTION = "voice_transcription"
    WEB_SEARCH = "web_search"
    MCP_ADD = "mcp_add"
    HTTP_ENDPOINTS = "http_endpoints"
    VISION = "vision"


CORE_FEATURES: frozenset[str] = frozenset(code.value for code in FeatureCode)


@dataclass(frozen=True, slots=True)
class TariffLimits:
    """Numeric plan caps; None means unlimited in that dimension."""

    daily_tokens: int | None = None
    daily_user_messages: int | None = None
    daily_assistant_messages: int | None = None
    max_cron_jobs: int | None = None
    max_datasets: int | None = None
    max_memory_chars: int | None = None

    def normalized(self) -> "TariffLimits":
        return TariffLimits(
            daily_tokens=normalize_limit(self.daily_tokens, "daily_tokens"),
            daily_user_messages=normalize_limit(self.daily_user_messages, "daily_user_messages"),
            daily_assistant_messages=normalize_limit(
                self.daily_assistant_messages,
                "daily_assistant_messages",
            ),
            max_cron_jobs=normalize_limit(self.max_cron_jobs, "max_cron_jobs"),
            max_datasets=normalize_limit(self.max_datasets, "max_datasets"),
            max_memory_chars=normalize_limit(self.max_memory_chars, "max_memory_chars"),
        )


@dataclass(frozen=True, slots=True)
class TariffDefinition:
    """Complete operator input for creating or replacing one plan."""

    code: str
    title: str
    features: frozenset[str]
    limits: TariffLimits | None = None
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class Tariff:
    """One persisted plan and its feature and numeric limits."""

    id: str
    code: str
    title: str
    features: frozenset[str]
    limits: TariffLimits
    created_at: datetime
    updated_at: datetime
    is_default: bool = False
