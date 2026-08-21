"""Map instruction, dataset and memory rows to their public values."""

from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.validation import parse_schema
from octoforge_core.instructions.api import Instruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.memory.api import Memory


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
    )


def to_dataset(row: DatasetRow) -> Dataset:
    return Dataset(
        row.id,
        row.owner_user_id,
        row.name,
        row.description,
        parse_schema(row.schema),
        row.usage_notes,
        row.retention,
        row.version,
        row.created_at,
        row.updated_at,
    )


def to_record(row: DatasetRecordRow) -> DatasetRecord:
    return DatasetRecord(row.id, row.dataset_id, row.owner_user_id, row.payload, row.created_at)


def to_memory(row: InstructionRow) -> Memory:
    return Memory(
        row.id,
        row.owner_id,
        row.title,
        row.content,
        tuple(row.tags),
        row.created_at,
        row.updated_at,
    )
