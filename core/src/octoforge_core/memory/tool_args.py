"""Validation of model-supplied memory tool arguments."""

from octoforge_core.tools.errors import ToolArgumentsError


def non_empty_string(raw: object, argument: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be a non-empty string")
    return raw


def tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(tag, str) for tag in raw):
        raise ToolArgumentsError("tags must be an array of strings")
    return tuple(raw)
