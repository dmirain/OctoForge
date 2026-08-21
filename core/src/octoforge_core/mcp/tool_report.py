"""Human-readable mcp_add outcomes."""

from octoforge_core.mcp.api import McpServer
from octoforge_core.mcp.skills import SkillPattern
from octoforge_core.mcp.sync import SyncOutcome, mirror_title
from octoforge_core.mcp.tool_contract import MAX_LISTED_TOOLS, SECRET_HINT_TEMPLATE


def ready_report(server: McpServer, outcome: SyncOutcome) -> str:
    listed = ", ".join(
        mirror_title(server.name, tool_name) for tool_name in outcome.tool_names[:MAX_LISTED_TOOLS]
    )
    skill_note = ""
    if outcome.skill is not None and outcome.skill is not SkillPattern.UNRECOGNIZED:
        skill_note = (
            f" A usage skill 'mcp/{server.name}' was written "
            f"({outcome.skill.value} shape) - recall finds it without a type filter."
        )
    report = (
        f"MCP server '{server.name}' ready: {outcome.tools} tools mirrored as endpoint "
        "records (find them with recall(type=endpoint), execute with external_call)."
        + (f" Tools: {listed}" if listed else "")
        + skill_note
    )
    return with_secret_hint(server, report)


def with_secret_hint(server: McpServer, report: str) -> str:
    if server.auth_secret_code is None:
        return report
    return f"{report}\n{SECRET_HINT_TEMPLATE.format(code=server.auth_secret_code)}"
