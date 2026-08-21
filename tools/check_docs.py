#!/usr/bin/env python
"""Fail when documentation paths or internal links no longer resolve."""

import argparse
import sys
from pathlib import Path

from docs_links import check_links
from docs_paths import check_paths
from docs_problems import Problem, markdown_files

REPO_ROOT = Path(__file__).resolve().parent.parent


def check(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    for doc in markdown_files(root):
        text = doc.read_text(encoding="utf-8")
        problems += check_paths(doc, text, root)
        problems += check_links(doc, text)
    return problems


def main() -> int:
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
        print(
            f"docs ok: {len(markdown_files(REPO_ROOT))} file(s), all references resolve"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
