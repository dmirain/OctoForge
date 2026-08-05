"""Age-based retention: how long each kind of row is kept, if at all.

Three tables grow and never shrink on their own. `messages` gains a row per
turn, `tasks` keeps every completed background job, and `exchanges` keeps every
settled obligation — all of them by design, because history is the product.
Eventually somebody has to decide how much of it to keep.

That decision is not ours to make, so **every limit defaults to "keep
everything" and nothing is deleted until an operator says so.** A retention
policy silently switched on by an upgrade would destroy data the installation
believed it had.

What is deliberately NOT here: retention for instructions, datasets and their
records. Those are things a user wrote on purpose — a skill, a memory, a food
diary — and deleting them on a timer would be deleting the user's work. Only
transcript-shaped data ages out.

The sweep is age-based and leaves the newest rows alone regardless of volume,
which is the property that makes it safe to run unattended: a quiet week never
empties a dialog.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

# `None` everywhere: an installation that never configures retention keeps
# every row forever, exactly as it did before this module existed.
KEEP_FOREVER = None


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How many days of each kind of row to keep; None means forever.

    Separate knobs rather than one, because the three age very differently. A
    transcript is what the agent reconstructs a dialog from and is the last
    thing to drop; a settled exchange is bookkeeping; a delivered task is
    little more than an audit trail once its result has been handed over.
    """

    messages_days: int | None = KEEP_FOREVER
    exchanges_days: int | None = KEEP_FOREVER
    tasks_days: int | None = KEEP_FOREVER
    # the usage ledger is transcript-shaped too: an event is an audit line,
    # and limit checks only ever read the current day
    usage_days: int | None = KEEP_FOREVER

    def enabled(self) -> bool:
        """Whether anything at all would be deleted."""
        return any(
            days is not None
            for days in (
                self.messages_days,
                self.exchanges_days,
                self.tasks_days,
                self.usage_days,
            )
        )

    def cutoff(self, days: int | None, now: datetime | None = None) -> datetime | None:
        """The timestamp before which rows may go, or None to keep everything."""
        if days is None:
            return None
        return (now or utc_now()) - timedelta(days=days)

    def describe(self) -> str:
        """One line for the startup report."""
        if not self.enabled():
            return "off — every row is kept"
        parts = [
            f"{name} {days}d"
            for name, days in (
                ("messages", self.messages_days),
                ("exchanges", self.exchanges_days),
                ("tasks", self.tasks_days),
                ("usage", self.usage_days),
            )
            if days is not None
        ]
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What one sweep removed, per table."""

    messages: int = 0
    exchanges: int = 0
    tasks: int = 0
    usage: int = 0

    def total(self) -> int:
        return self.messages + self.exchanges + self.tasks + self.usage

    def describe(self) -> str:
        return (
            f"messages={self.messages} exchanges={self.exchanges} "
            f"tasks={self.tasks} usage={self.usage}"
        )
