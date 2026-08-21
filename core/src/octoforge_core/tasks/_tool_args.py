"""Shared validation for model-supplied task tool arguments."""

from octoforge_core.tools.errors import ToolArgumentsError


def non_empty_string(raw: object, argument: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be a non-empty string")
    return raw
