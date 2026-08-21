"""Detection and resolution of repository paths named in documentation."""

import re
from pathlib import Path

from docs_problems import Problem, line_of

SOURCE_ROOTS = (
    Path("."),
    Path("core/src"),
    Path("server/src"),
    Path("deploy/src"),
    Path("surfaces/telegram/src"),
    Path("surfaces/console/src"),
    Path("surfaces/webui/src"),
    Path("core/src/octoforge_core"),
    Path("server/src/octoforge_server"),
    Path("deploy/src/octoforge_deploy"),
    Path("surfaces/telegram/src/octoforge_telegram"),
)
PATH_ALIASES = (
    ("core/", "core/src/octoforge_core/"),
    ("server/", "server/src/octoforge_server/"),
    ("telegram/", "surfaces/telegram/src/octoforge_telegram/"),
    ("console/", "surfaces/console/src/octoforge_console/"),
    ("webui/", "surfaces/webui/src/octoforge_webui/"),
    ("deploy/", "deploy/src/octoforge_deploy/"),
)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
PATH_CANDIDATE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+/?$")
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


def check_paths(doc: Path, text: str, root: Path) -> list[Problem]:
    problems = []
    for match in CODE_SPAN.finditer(text):
        value = match.group(1).strip()
        if _is_path_like(value) and not _path_exists(root, value):
            problems.append(
                Problem(doc, line_of(text, match.start()), "missing path", value)
            )
    return problems


def _is_path_like(value: str) -> bool:
    if not PATH_CANDIDATE.match(value) or value.startswith(IGNORED_PREFIXES):
        return False
    return value.endswith("/") or value.endswith(CHECKED_SUFFIXES)


def _path_exists(root: Path, value: str) -> bool:
    relative = value.rstrip("/")
    if any((root / source / relative).exists() for source in SOURCE_ROOTS):
        return True
    return any(
        relative.startswith(prefix)
        and (root / replacement / relative[len(prefix) :]).exists()
        for prefix, replacement in PATH_ALIASES
    )
