"""Errors raised by the tools subsystem."""


class DuplicateToolError(Exception):
    """Raised when registering a tool name that already exists."""


class ToolNotFoundError(Exception):
    """Raised when a tool name is not present in the registry."""


class ToolArgumentsError(Exception):
    """Raised when LLM-provided tool arguments fail validation."""
