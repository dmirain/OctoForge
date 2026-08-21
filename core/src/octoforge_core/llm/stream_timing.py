"""Diagnostic timing of one streamed answer."""

import logging
import time
from dataclasses import dataclass, field

from octoforge_core.llm.events import StreamEvent, TextDelta

logger = logging.getLogger(__name__)
STREAM_GAP_LOG_SECONDS = 3.0


@dataclass(slots=True)
class StreamTiming:
    started: float = field(default_factory=time.monotonic)
    last_chunk: float = 0.0
    first_content: float | None = None
    largest_gap: float = 0.0
    largest_gap_at_chars: int = 0
    content_chars: int = 0
    chunks: int = 0

    def observe(self, events: list[StreamEvent]) -> None:
        now = time.monotonic()
        if self.chunks > 0:
            self._observe_gap(now - self.last_chunk)
        self.last_chunk = now
        self.chunks += 1
        for event in events:
            if isinstance(event, TextDelta):
                if self.first_content is None:
                    self.first_content = now - self.started
                self.content_chars += len(event.text)

    def log_summary(self, ignored_fields: dict[str, int]) -> None:
        logger.info(
            "stream timing: %.1fs total, first content at %s, %d chunks, %d chars, "
            "largest gap %.1fs after %d chars, ignored delta fields %s",
            time.monotonic() - self.started,
            f"{self.first_content:.1f}s" if self.first_content is not None else "never",
            self.chunks,
            self.content_chars,
            self.largest_gap,
            self.largest_gap_at_chars,
            ignored_fields or "none",
        )

    def _observe_gap(self, gap: float) -> None:
        if gap > self.largest_gap:
            self.largest_gap = gap
            self.largest_gap_at_chars = self.content_chars
        if gap >= STREAM_GAP_LOG_SECONDS:
            logger.info(
                "stream stalled: %.1fs of silence ended after %d visible chars",
                gap,
                self.content_chars,
            )
