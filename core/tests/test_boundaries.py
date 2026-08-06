"""Import-boundary guards: framework packages must not depend on domain modules.

The 2026-07-27 audit fixed the arrows (tools/ imported tasks/, cron/ imported
agent/); these tests keep them fixed — a reintroduced framework→domain import
fails here with a readable message instead of surfacing as a review comment.
"""

import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "octoforge_core"
IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+octoforge_core\.([a-z_]+)", re.MULTILINE)

# shared vocabulary any package may use
FRAMEWORK = {"domain", "ports", "time", "config", "errors", "db", "tools", "llm"}
DOMAIN_MODULES = {
    "admin",
    "agent",
    "context",
    "cron",
    "datasets",
    "instructions",
    "memory",
    "net",
    "search",
    "secrets",
    "settings",
    "tariffs",
    "tasks",
}


def imports_of(package: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in (CORE / package).rglob("*.py"):
        for target in IMPORT_PATTERN.findall(path.read_text(encoding="utf-8")):
            found.add((str(path.relative_to(CORE)), target))
    return found


def test_tools_framework_imports_no_domain_module() -> None:
    """tools/ is 'framework only' by its own charter — keep it that way."""
    offending = {(path, target) for path, target in imports_of("tools") if target in DOMAIN_MODULES}

    assert offending == set()


def test_cron_never_imports_the_actor() -> None:
    """cron knows its CronWaker port; the actor implementation satisfies it

    from the composition root — the domain module must not reach into agent/.
    """
    offending = {(path, target) for path, target in imports_of("cron") if target == "agent"}

    assert offending == set()


def test_db_is_framework_only() -> None:
    """db/ holds Base, engine and migrations — domain tables live in their modules.

    The one sanctioned exception: engine.py and migrations/env.py import the
    model modules for their registration side effect (`Base.metadata` must
    know every table before create_all / autogenerate).
    """
    registration_files = {"db/engine.py", "db/migrations/env.py"}
    offending = set()
    for path, target in imports_of("db"):
        if target not in DOMAIN_MODULES:
            continue
        if path in registration_files:
            continue
        offending.add((path, target))

    assert offending == set()


def test_no_module_imports_a_neighbours_tools() -> None:
    """Tool implementations are per-module; the cross-module contract is api.py.

    tasks/tools.py used to import 12 names from cron/tools.py — the coupling
    the audit moved onto the cron.api boundary (create_job, format_job, the
    shared schema fragments). Composition assembles tools from every module;
    modules themselves never reach into a neighbour's tool implementation.
    """
    offending = set()
    for module in sorted(DOMAIN_MODULES):
        for path, target in imports_of(module):
            if target in DOMAIN_MODULES and target != module:
                text = (CORE / path).read_text(encoding="utf-8")
                if re.search(rf"from octoforge_core\.{target}\.tools import", text):
                    offending.add((path, target))

    assert offending == set()
