"""Transcribe one Telegram recording and submit its narrative message."""

import asyncio
from dataclasses import dataclass

from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_core.media.api import (
    AudioDuration,
    MediaOutcome,
    MediaUnderstanding,
    TranscriptionRequest,
)

from octoforge_telegram.gateway import DialogSubmission, GatewayRegistry
from octoforge_telegram.images import REF_PREFIX
from octoforge_telegram.models import TelegramAudioRef, TelegramMessage
from octoforge_telegram.poller_activity import IngestionActivity
from octoforge_telegram.poller_format import MediaNarrative, compose_voice


@dataclass(frozen=True, slots=True)
class VoiceServices:
    registry: GatewayRegistry
    media: MediaUnderstanding
    slots: asyncio.Semaphore
    activity: IngestionActivity
    max_seconds: float


@dataclass(frozen=True, slots=True)
class VoiceSubmission:
    message: TelegramMessage
    user_id: str
    chat_id: int
    audio: TelegramAudioRef
    kind: MessageKind


class VoiceSubmitter:
    def __init__(self, services: VoiceServices) -> None:
        self._registry = services.registry
        self._media = services.media
        self._slots = services.slots
        self._activity = services.activity
        self._max_seconds = services.max_seconds

    async def submit(self, request: VoiceSubmission) -> tuple[MediaOutcome, str]:
        async with self._slots, self._activity.showing(request.chat_id, request.kind):
            result = await self._media.transcribe(
                TranscriptionRequest(
                    request.user_id,
                    f"{REF_PREFIX}{request.audio.file_id}",
                    AudioDuration(
                        request.audio.duration_seconds,
                        1,
                        self._max_seconds,
                    ),
                )
            )
        if not result.ok:
            return result.outcome, ""
        transcript = result.text.strip()
        if not transcript:
            return result.outcome, ""
        forwarded = request.message.forward_origin is not None
        origin = (
            request.message.forward_origin.display_name
            if request.message.forward_origin is not None
            else ""
        )
        bridge = await self._registry.gateway_for(request.user_id, request.chat_id)
        await bridge.handle_text(
            DialogSubmission(
                compose_voice(
                    transcript,
                    MediaNarrative(request.message.body or "", origin, forwarded),
                ),
                client_message_id=str(request.message.message_id),
                reply_to_message_id=(
                    request.message.reply_to_message.message_id
                    if request.message.reply_to_message is not None
                    else None
                ),
                kind=request.kind,
                origin=(origin or None) if forwarded else None,
                attachments=(
                    Attachment(
                        AttachmentKind.AUDIO,
                        f"{REF_PREFIX}{request.audio.file_id}",
                    ),
                ),
            )
        )
        return result.outcome, transcript
