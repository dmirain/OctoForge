"""API schemas for dialog endpoints."""

from pydantic import BaseModel


class PostMessageRequest(BaseModel):
    """Incoming user message."""

    content: str


class AckResponse(BaseModel):
    """Accepted-for-processing acknowledgement."""

    status: str
