"""Validated console payloads for tariff mutations."""

from pydantic import BaseModel, Field


class SetTariffRequest(BaseModel):
    code: str
    title: str
    features: list[str] = Field(default_factory=list)
    daily_tokens: int | None = None
    daily_user_messages: int | None = None
    daily_assistant_messages: int | None = None
    max_cron_jobs: int | None = None
    max_datasets: int | None = None
    max_memory_chars: int | None = None
    is_default: bool = False


class AssignTariffRequest(BaseModel):
    user_id: str
    code: str | None = None
