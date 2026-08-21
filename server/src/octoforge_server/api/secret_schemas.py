"""Validated request bodies for the self-service secrets form."""

from pydantic import BaseModel, Field


class SetSecretRequest(BaseModel):
    """Add or replace one secret; the value never appears in responses."""

    token: str
    code: str
    value: str = Field(repr=False)
    allowed_host: str
    description: str
    placements: list[str] = []
    transform: str | None = None


class DeleteSecretRequest(BaseModel):
    token: str
    code: str


class SessionRequest(BaseModel):
    """Form token carried in a body so proxies and history do not log it."""

    token: str
