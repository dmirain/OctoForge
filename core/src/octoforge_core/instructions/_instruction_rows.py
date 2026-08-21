"""Shared SQL row predicates and DTO mapping for instruction persistence."""

from sqlalchemy import ColumnElement

from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import Instruction, InstructionType


def owner_clause(owner_id: str | None) -> ColumnElement[bool]:
    """Match ownership exactly; None names the public record."""
    if owner_id is None:
        return InstructionRow.owner_id.is_(None)
    return InstructionRow.owner_id == owner_id


def to_instruction(row: InstructionRow) -> Instruction:
    return Instruction(
        id=row.id,
        type=InstructionType(row.type),
        title=row.title,
        content=row.content,
        tags=tuple(row.tags),
        version=row.version,
        usage_count=row.usage_count,
        success_count=row.success_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        system=row.system,
        owner_id=row.owner_id,
        author_id=row.author_id,
    )
