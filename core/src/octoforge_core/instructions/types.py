"""Records and errors shared across the instructions module boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class InstructionType(StrEnum):
    """Kind of stored instruction record."""

    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    ENDPOINT = "endpoint"
    MEMORY = "memory"


class InstructionNotFoundError(Exception):
    """No instruction matches the requested identity."""


class SystemInstructionError(Exception):
    """An agent-facing write targeted a registry-owned record."""


@dataclass(frozen=True, slots=True)
class Instruction:
    """One instruction record without its storage-specific embedding."""

    id: str
    type: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]
    version: int
    usage_count: int
    success_count: int
    created_at: datetime
    updated_at: datetime
    system: bool = False
    owner_id: str | None = None
    author_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """An instruction returned by search with its relevance score."""

    instruction: Instruction
    score: float


@dataclass(frozen=True, slots=True)
class EmbeddedInstruction:
    """An instruction together with its stored embedding."""

    instruction: Instruction
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class InstructionDraft:
    """Store upsert input after content has been embedded and scoped."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]
    embedding: tuple[float, ...]
    embedding_model: str | None = None
    system: bool = False
    owner_id: str | None = None
    author_id: str | None = None
