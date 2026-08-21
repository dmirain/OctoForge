"""Plan-check, understand and meter a user's media in that order."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.media.api import MediaOutcome, MediaResult, TranscriptionRequest
from octoforge_core.media.prompt import INGESTION_PROMPT
from octoforge_core.speech.api import AudioResolver, TranscriptionClient
from octoforge_core.tariffs.api import (
    FeatureCode,
    LimitGate,
    UsageEvent,
    UsageKind,
    UsageOrigin,
)
from octoforge_core.vision.api import ImageResolver, VisionClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImageUnderstanding:
    client: VisionClient
    resolver: ImageResolver


@dataclass(frozen=True, slots=True)
class AudioUnderstanding:
    client: TranscriptionClient
    resolver: AudioResolver


class MediaService:
    def __init__(
        self,
        images: ImageUnderstanding | None = None,
        audio: AudioUnderstanding | None = None,
        limits: LimitGate | None = None,
    ) -> None:
        self._images = images
        self._audio = audio
        self._limits = limits

    @property
    def describes_images(self) -> bool:
        return self._images is not None

    @property
    def transcribes_audio(self) -> bool:
        return self._audio is not None

    async def describe(self, user_id: str, refs: Sequence[str]) -> tuple[MediaResult, ...]:
        if not self.describes_images:
            return _all(refs, MediaOutcome.UNAVAILABLE)
        if not await self._allows(user_id, FeatureCode.VISION):
            return _all(refs, MediaOutcome.REFUSED_BY_PLAN)
        described = await asyncio.gather(
            *(self._describe_one(ref) for ref in refs), return_exceptions=False
        )
        results = tuple(described)
        await self._record(user_id, UsageKind.VISION, sum(1 for item in results if item.ok))
        return results

    async def transcribe(self, request: TranscriptionRequest) -> MediaResult:
        if not self.transcribes_audio:
            return MediaResult(MediaOutcome.UNAVAILABLE)
        if not await self._allows(request.user_id, FeatureCode.VOICE_TRANSCRIPTION):
            return MediaResult(MediaOutcome.REFUSED_BY_PLAN)
        seconds = request.duration.seconds
        if seconds is not None:
            if seconds < request.duration.minimum:
                return MediaResult(MediaOutcome.TOO_SHORT)
            if seconds > request.duration.maximum:
                return MediaResult(MediaOutcome.TOO_LONG)
        assert self._audio is not None
        try:
            recording = await self._audio.resolver.fetch(request.ref)
            transcript = await self._audio.client.transcribe(recording)
        except Exception as exc:
            logger.warning("transcription failed for %s: %s", request.ref, exc)
            return MediaResult(MediaOutcome.UNAVAILABLE)
        await self._record(request.user_id, UsageKind.VOICE_TRANSCRIPTION, seconds or 0)
        return MediaResult(MediaOutcome.OK, transcript)

    async def _describe_one(self, ref: str) -> MediaResult:
        assert self._images is not None
        try:
            image = await self._images.resolver.fetch(ref)
            description = await self._images.client.look((image,), INGESTION_PROMPT)
        except Exception as exc:  # broad for the same reason as `transcribe`
            logger.warning("description failed for %s: %s", ref, exc)
            return MediaResult(MediaOutcome.UNAVAILABLE)
        return MediaResult(MediaOutcome.OK, description)

    async def _allows(self, user_id: str, feature: FeatureCode) -> bool:
        if self._limits is None:
            return True
        return await self._limits.allows(user_id, feature)

    async def _record(self, user_id: str, kind: UsageKind, quantity: int) -> None:
        if self._limits is None or quantity <= 0:
            return
        try:
            await self._limits.record(
                UsageEvent(
                    user_id=user_id,
                    kind=kind,
                    origin=UsageOrigin.INTERACTIVE,
                    quantity=quantity,
                )
            )
        except Exception:
            logger.exception("usage metering failed: user=%s kind=%s", user_id, kind)


def _all(refs: Sequence[str], outcome: MediaOutcome) -> tuple[MediaResult, ...]:
    return tuple(MediaResult(outcome) for _ in refs)
