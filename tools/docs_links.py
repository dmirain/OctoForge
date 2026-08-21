"""Validation of relative Markdown links and heading anchors."""

import re
from pathlib import Path

from docs_problems import Problem, line_of

MARKDOWN_LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
ANCHOR_ALLOWED = re.compile(r"[^a-z0-9\- ]")


def heading_anchors(text: str) -> set[str]:
    return {
        ANCHOR_ALLOWED.sub("", heading.strip().lower()).replace(" ", "-")
        for heading in HEADING.findall(text)
    }


def check_links(doc: Path, text: str) -> list[Problem]:
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
        elif fragment and resolved.suffix == ".md":
            other = heading_anchors(resolved.read_text(encoding="utf-8"))
            if fragment not in other:
                problems.append(
                    Problem(doc, line, "missing anchor", f"{path}#{fragment}")
                )
    return problems
