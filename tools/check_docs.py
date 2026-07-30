#!/usr/bin/env python
"""Fail when the documentation points at something that no longer exists.

    python tools/check_docs.py            # check docs/ and the root markdown files
    python tools/check_docs.py --quiet    # only report problems

Two classes of rot are mechanical, so they are checked mechanically:

* **Repository paths.** Documentation names files and directories (`core/src/...`,
  `tools/quickstart.py`) in prose and in code anchors. A rename leaves the prose
  pointing at nothing, and nobody notices until a reader does.
* **Internal links.** `[text](../reference/cron.md)` and `#anchors` inside the
  same page break the same silent way.

What this cannot check is whether a sentence is *true* — see docs/CONVENTIONS.md.

`docs/archive/` is exempt: it is frozen historical material and is expected to
name files that have since moved.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
# Documentation and agent guidance name module paths two ways: from the
# repository root (`core/src/octoforge_core/agent/loop.py`) and package-relative
# (`agent/loop.py`, `telegram/bridge.py`), which is how the code itself and the
# conventions files refer to neighbours. Both resolve.
SOURCE_ROOTS = (
    Path("."),
    Path("core/src"),
    Path("web/src"),
    Path("core/src/octoforge_core"),
    Path("web/src/octoforge_web"),
)
# The two projects are also referred to by name — `core/composition.py`,
# `web/telegram/bridge.py` — meaning "this module inside that project's
# package". Applied only after the plain roots fail.
PATH_ALIASES = (
    ("core/", "core/src/octoforge_core/"),
    ("web/", "web/src/octoforge_web/"),
)
EXEMPT_DIRS = (DOCS_DIR / "archive",)
ROOT_MARKDOWN = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
)

# `path/like/this.py`, `dir/name/` — only inside backticks, so prose about
# "the agent module" is never mistaken for a path.
CODE_SPAN = re.compile(r"`([^`\n]+)`")
PATH_CANDIDATE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+/?$")
# Extensions worth checking: everything else in backticks is prose, config keys
# or shell fragments that merely contain a slash.
CHECKED_SUFFIXES = (
    ".py",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".mako",
)
# Paths that legitimately do not exist on disk: examples, URLs, runtime artifacts.
IGNORED_PREFIXES = (
    "http://",
    "https://",
    "example/",
    "path/to/",
    "your/",
    "etc/octoforge/",
    "var/lib/",
    "usr/",
    "tmp/",
    "root/",
    "home/",
)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
ANCHOR_ALLOWED = re.compile(r"[^a-z0-9\- ]")


@dataclass(frozen=True, slots=True)
class Problem:
    """One broken reference: where it is and what is wrong."""

    doc: Path
    line: int
    kind: str
    detail: str

    def render(self, root: Path) -> str:
        """Render as an editor-friendly `file:line: message` string."""
        return f"{self.doc.relative_to(root)}:{self.line}: {self.kind}: {self.detail}"


def markdown_files(root: Path) -> list[Path]:
    """Every documentation file to check, archive excluded."""
    docs = [
        path
        for path in sorted((root / "docs").rglob("*.md"))
        if not any(exempt in path.parents for exempt in EXEMPT_DIRS)
    ]
    return docs + [root / name for name in ROOT_MARKDOWN if (root / name).exists()]


def heading_anchors(text: str) -> set[str]:
    """GitHub-style anchor slugs of every heading in the document."""
    anchors = set()
    for heading in HEADING.findall(text):
        slug = ANCHOR_ALLOWED.sub("", heading.strip().lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors


def line_of(text: str, index: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, index) + 1


def is_path_like(value: str) -> bool:
    """Whether a code span looks like a repository path worth checking."""
    if not PATH_CANDIDATE.match(value) or value.startswith(IGNORED_PREFIXES):
        return False
    return value.endswith("/") or value.endswith(CHECKED_SUFFIXES)


def path_exists(root: Path, value: str) -> bool:
    """Whether the path resolves from the repository root, a package root or an alias."""
    relative = value.rstrip("/")
    if any((root / source / relative).exists() for source in SOURCE_ROOTS):
        return True
    return any(
        relative.startswith(prefix)
        and (root / replacement / relative[len(prefix) :]).exists()
        for prefix, replacement in PATH_ALIASES
    )


def check_paths(doc: Path, text: str, root: Path) -> list[Problem]:
    """Every backticked repository path must exist."""
    problems = []
    for match in CODE_SPAN.finditer(text):
        value = match.group(1).strip()
        if not is_path_like(value):
            continue
        if not path_exists(root, value):
            problems.append(
                Problem(doc, line_of(text, match.start()), "missing path", value)
            )
    return problems


def check_links(doc: Path, text: str, root: Path) -> list[Problem]:
    """Every relative markdown link must resolve; same-page anchors must exist."""
    problems = []
    anchors = heading_anchors(text)
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1)
        line = line_of(text, match.start())
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            if target[1:] not in anchors:
                problems.append(Problem(doc, line, "missing anchor", target))
            continue
        path, _, fragment = target.partition("#")
        resolved = (doc.parent / path).resolve()
        if not resolved.exists():
            problems.append(Problem(doc, line, "broken link", target))
            continue
        if fragment and resolved.suffix == ".md":
            other = heading_anchors(resolved.read_text(encoding="utf-8"))
            if fragment not in other:
                problems.append(
                    Problem(doc, line, "missing anchor", f"{path}#{fragment}")
                )
    _ = root
    return problems


def check(root: Path) -> list[Problem]:
    """Check every documentation file and return the problems found."""
    problems: list[Problem] = []
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        problems += check_paths(doc, text, root)
        problems += check_links(doc, text, root)
    return problems


def main() -> int:
    """Run the checks; non-zero exit means the documentation lies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="print nothing when clean")
    args = parser.parse_args()

    problems = check(REPO_ROOT)
    if problems:
        print(f"{len(problems)} documentation problem(s):", file=sys.stderr)
        for problem in problems:
            print(problem.render(REPO_ROOT), file=sys.stderr)
        return 1
    if not args.quiet:
        checked = len(markdown_files(REPO_ROOT))
        print(f"docs ok: {checked} file(s), every path and internal link resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
