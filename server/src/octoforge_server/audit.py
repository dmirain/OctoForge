"""Audit trail of operator actions.

Everything an operator does through the console or the in-chat admin tool
changes what other people see: publishing somebody's private record, deleting a
dialog, revoking access. Until now those left no trace beyond whatever the
handler happened to log, so "who published this?" had no answer.

This is deliberately a log, not a table: the events are few, an operator with
database access could edit a table anyway, and a log line ships to wherever the
rest of the logs already go. The format is stable and greppable — `audit
action=... actor=... target=... outcome=...` — so a collector can parse it.

Never log a value: an audit line names what was touched, not its contents.
"""

import logging

logger = logging.getLogger("octoforge.audit")

UNKNOWN_TARGET = "-"


def record(action: str, actor: str, target: str = UNKNOWN_TARGET, outcome: str = "ok") -> None:
    """Write one audit line.

    `actor` is the operator credential's username for HTTP, or `tg:<id>` for the
    in-chat admin tool; `target` is the id of whatever was acted upon.
    """
    logger.info("audit action=%s actor=%s target=%s outcome=%s", action, actor, target, outcome)
