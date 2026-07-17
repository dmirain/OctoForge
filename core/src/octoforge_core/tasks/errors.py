"""Errors raised by the tasks subsystem."""


class TaskNotFoundError(Exception):
    """Raised when a task id is not present in the store."""
