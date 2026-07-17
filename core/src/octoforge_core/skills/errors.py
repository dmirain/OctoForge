"""Errors raised by the skills subsystem."""


class DuplicateSkillError(Exception):
    """Raised when registering a skill name that already exists."""


class SkillNotFoundError(Exception):
    """Raised when a skill name is not present in the registry."""


class SkillArgumentsError(Exception):
    """Raised when LLM-provided skill arguments fail validation."""
