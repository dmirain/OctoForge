"""Documentation files, broken-reference values and line reporting."""

from dataclasses import dataclass
from pathlib import Path

ROOT_MARKDOWN = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
)


@dataclass(frozen=True, slots=True)
class Problem:
    doc: Path
    line: int
    kind: str
    detail: str

    def render(self, root: Path) -> str:
        return f"{self.doc.relative_to(root)}:{self.line}: {self.kind}: {self.detail}"


def markdown_files(root: Path) -> list[Path]:
    docs = sorted((root / "docs").rglob("*.md"))
    return docs + [root / name for name in ROOT_MARKDOWN if (root / name).exists()]


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1
