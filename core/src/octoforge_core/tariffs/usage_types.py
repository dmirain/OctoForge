"""Usage ledger values and budget verdicts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UsageKind(StrEnum):
    LLM_ANSWER = "llm_answer"
    LLM_ROUTING = "llm_routing"
    LLM_COMPACTION = "llm_compaction"
    VOICE_TRANSCRIPTION = "voice_transcription"
    VISION = "vision"
    USER_MESSAGE = "user_message"


class UsageOrigin(StrEnum):
    INTERACTIVE = "interactive"
    CRON = "cron"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class UsageEvent:
    user_id: str
    kind: UsageKind
    origin: UsageOrigin
    prompt_tokens: int = 0
    completion_tokens: int = 0
    quantity: int = 0
    dialog_id: str | None = None
    exchange_id: str | None = None
    task_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    user_messages: int = 0
    assistant_messages: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LimitVerdict:
    allowed: bool
    reason: str | None = None
    used: int = 0
    limit: int = 0

    @classmethod
    def ok(cls) -> "LimitVerdict":
        return cls(allowed=True)
