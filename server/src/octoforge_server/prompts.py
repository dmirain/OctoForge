"""File-backed PromptProvider: named prompts read from external files."""

import logging
from collections.abc import Mapping
from pathlib import Path

from octoforge_core.agent.prompts import PromptProvider

logger = logging.getLogger(__name__)


class FilePromptProvider:
    """Reads named prompts from files on every get(), falling back on failure.

    Reading on every call keeps the file the live source of truth: editing it
    changes the next dialog turn without a restart. Names without a
    configured file, and unreadable files, fall back to the wrapped provider
    (a warning is logged for the latter).
    """

    def __init__(self, files: Mapping[str, Path], fallback: PromptProvider) -> None:
        self._files = dict(files)
        self._fallback = fallback

    def get(self, name: str) -> str:
        """Return the file's content for `name`, or the fallback prompt."""
        path = self._files.get(name)
        if path is None:
            return self._fallback.get(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "prompt file unreadable, using the fallback: name=%s path=%s", name, path
            )
            return self._fallback.get(name)
