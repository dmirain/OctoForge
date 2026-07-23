"""Detector for Markdown constructs that the Telegram-HTML path degrades.

Plain prose, bold/italic, simple lists and quotes render fine through the legacy
HTML conversion; tables, task lists, collapsible details and block math do not.
A final answer carrying any of those is upgraded to a native Rich Message
(Bot API 10.1) instead of being left on the degraded rendering.
"""

import re

_TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_TASK_ITEM_RE = re.compile(r"^[-*]\s+\[[ xX]\]\s", re.MULTILINE)
_DETAILS_RE = re.compile(r"<details[\s>]", re.IGNORECASE)
_MATH_BLOCK_RE = re.compile(r"\$\$[^\$]+\$\$", re.DOTALL)


def needs_rich_message(text: str) -> bool:
    """True when the Markdown holds a construct worth a native Rich Message."""
    return (
        _TABLE_SEPARATOR_RE.search(text) is not None
        or _TASK_ITEM_RE.search(text) is not None
        or _DETAILS_RE.search(text) is not None
        or _MATH_BLOCK_RE.search(text) is not None
    )
