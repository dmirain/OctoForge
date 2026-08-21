"""Actor lifecycle markers carried on the loop event stream."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessStarted:
    """A process is about to emit text, including its transport reply target."""

    process_id: str
    title: str
    source_client_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCompleted:
    """A process reached a terminal TaskStatus value."""

    process_id: str
    title: str
    status: str
