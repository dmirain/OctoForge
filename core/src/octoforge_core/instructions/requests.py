"""Typed requests that keep instruction interfaces small and unambiguous."""

from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.instructions.types import EmbeddedInstruction, InstructionType, SearchHit


@dataclass(frozen=True, slots=True)
class InstructionDefinition:
    """Content and classification supplied by an author or integration."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstructionSearchRequest:
    """A ranked lookup independent of who is allowed to see the records."""

    query: str
    limit: int
    kind: InstructionType | None = None


@dataclass(frozen=True, slots=True)
class InstructionVectorQuery:
    """Storage-side vector candidate request with visibility and kind filters."""

    embedding: tuple[float, ...]
    limit: int
    user_id: str | None
    kinds: tuple[InstructionType, ...] = ()


@dataclass(frozen=True, slots=True)
class InstructionTextQuery:
    """Storage-side lexical candidate request with visibility and kind filters."""

    text: str
    limit: int
    user_id: str | None
    kinds: tuple[InstructionType, ...] = ()


@dataclass(frozen=True, slots=True)
class InstructionRankingRequest:
    candidates: list[EmbeddedInstruction]
    query: str
    embedding: tuple[float, ...]
    limit: int


@dataclass(frozen=True, slots=True)
class InstructionRerankingRequest:
    hits: list[SearchHit]
    scores: tuple[float, ...]
    query: str
    limit: int


@dataclass(frozen=True, slots=True)
class InstructionFusionRequest:
    rankings: Sequence[Sequence[EmbeddedInstruction]]
    query: str
    limit: int
