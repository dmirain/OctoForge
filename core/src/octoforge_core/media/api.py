"""The checked and metered boundary through which surfaces turn media into text."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class MediaOutcome(StrEnum):
    """What happened to one piece of media."""

    OK = "ok"
    #: The user's tariff does not include the feature. Spoken to the user.
    REFUSED_BY_PLAN = "refused_by_plan"
    #: Below the surface's minimum duration (a mis-tap, not a message).
    TOO_SHORT = "too_short"
    #: Over the surface's maximum duration.
    TOO_LONG = "too_long"
    #: Configured off, the reference would not resolve, or the model failed.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MediaResult:
    """The outcome for one reference, plus the text when there is any."""

    outcome: MediaOutcome
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is MediaOutcome.OK


@dataclass(frozen=True, slots=True)
class AudioDuration:
    seconds: int | None
    minimum: int
    maximum: float


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    user_id: str
    ref: str
    duration: AudioDuration


def refused_by_plan(results: Sequence[MediaResult]) -> bool:
    """Whether the plan is what stopped this batch.

    The distinction the caller cannot skip: a plan refusal earns the user a
    sentence explaining it, while everything else falls back quietly. One
    call asks the gate once, so a batch is refused whole or not at all — this
    only has to look at the first result, and reads the whole batch anyway to
    stay honest if that ever changes.
    """
    return bool(results) and all(item.outcome is MediaOutcome.REFUSED_BY_PLAN for item in results)


def none_understood(results: Sequence[MediaResult]) -> bool:
    """Whether nothing came back usable — the caller's cue to fall back.

    True for an empty batch as well: there is nothing to put in a message
    either way, and the caller's fallback path is what should run.
    """
    return not any(item.ok for item in results)


class MediaUnderstanding(Protocol):
    """The one way a surface turns media into text.

    Two implementations, one per deployment shape: the core service itself
    when the surface runs in the same process, and an HTTP client when
    ingestion is its own node. Neither is allowed to call a model directly.
    """

    async def describe(self, user_id: str, refs: Sequence[str]) -> tuple[MediaResult, ...]:
        """Describe each image, in order; one result per reference.

        Per-reference results rather than one string: an album where the
        third picture failed still delivers the other four, each in its own
        numbered slot.
        """
        ...

    async def transcribe(self, request: TranscriptionRequest) -> MediaResult:
        """Transcribe one recording.

        The duration bounds travel with the call instead of being applied by
        the caller, so that the plan check comes first: a user whose tariff
        has no voice must be told *that*, not that their recording was too
        short — no duration would have worked for them. `seconds` is what the
        transport declared, and `None` means it did not know.
        """
        ...
