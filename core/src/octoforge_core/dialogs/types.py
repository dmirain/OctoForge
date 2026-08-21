"""Dialog exchange, ownership and activity values."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class DialogNotFoundError(Exception):
    """Raised when a dialog id is absent."""


class ExchangeNotFoundError(Exception):
    """Raised when an exchange id is absent."""


class ExchangeStatus(StrEnum):
    COLLECTING = "collecting"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_USER = "awaiting_user"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    FAILED = "failed"


LIVE_EXCHANGE_STATUSES = (
    ExchangeStatus.COLLECTING,
    ExchangeStatus.OPEN,
    ExchangeStatus.IN_PROGRESS,
    ExchangeStatus.AWAITING_USER,
)
TITLE_MAX_LENGTH = 60


@dataclass(slots=True)
class Exchange:
    id: str
    dialog_id: str
    status: ExchangeStatus
    title: str
    created_at: datetime
    updated_at: datetime
    pending_question: str | None = None


ExchangeList = list[Exchange]


@dataclass(frozen=True, slots=True)
class DialogClaim:
    dialog_id: str
    owner: str
    generation: int
    heartbeat_at: datetime


DialogClaimList = list[DialogClaim]


@dataclass(frozen=True, slots=True)
class MessageStats:
    user_id: str
    user_messages: int
    user_chars: int
    agent_messages: int
    agent_chars: int


MessageStatsList = list[MessageStats]
ACTIVITY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class UserActivity:
    user_id: str
    last_user_message_at: datetime | None
    user_messages_since: int


UserActivityList = list[UserActivity]
