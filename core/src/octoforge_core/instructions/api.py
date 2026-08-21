"""Public boundary of the instructions module."""

from octoforge_core.instructions.ports import (
    InstructionLexicalSearch,
    InstructionService,
    InstructionStore,
    InstructionVectorSearch,
)
from octoforge_core.instructions.requests import (
    InstructionDefinition,
    InstructionSearchRequest,
    InstructionTextQuery,
    InstructionVectorQuery,
)
from octoforge_core.instructions.types import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
    SystemInstructionError,
)

__all__ = [
    "EmbeddedInstruction",
    "Instruction",
    "InstructionDefinition",
    "InstructionDraft",
    "InstructionLexicalSearch",
    "InstructionNotFoundError",
    "InstructionSearchRequest",
    "InstructionService",
    "InstructionStore",
    "InstructionTextQuery",
    "InstructionType",
    "InstructionVectorQuery",
    "InstructionVectorSearch",
    "SearchHit",
    "SystemInstructionError",
]
