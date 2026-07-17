"""API schemas for conversation endpoints."""

from pydantic import BaseModel


class CreateConversationResponse(BaseModel):
    """Response with the new conversation id."""

    id: str


class PostMessageRequest(BaseModel):
    """Incoming user message."""

    content: str


class AckResponse(BaseModel):
    """Accepted-for-processing acknowledgement."""

    status: str
