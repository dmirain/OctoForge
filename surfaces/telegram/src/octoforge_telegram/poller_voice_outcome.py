"""Voice transcription outcome notices and fallback behavior."""

from dataclasses import dataclass

from octoforge_core.media.api import MediaOutcome

from octoforge_telegram.client import SendMessage, TelegramClient
from octoforge_telegram.poller_material import MaterialDispatcher
from octoforge_telegram.poller_text import (
    VOICE_EMPTY_NOTICE,
    VOICE_PLAN_NOTICE,
    VOICE_TOO_LONG_TEMPLATE,
    VOICE_TOO_SHORT_NOTICE,
)
from octoforge_telegram.poller_voice import VoiceSubmission, VoiceSubmitter


@dataclass(frozen=True, slots=True)
class VoiceOutcomeServices:
    client: TelegramClient
    voice: VoiceSubmitter
    material: MaterialDispatcher
    max_seconds: float


class VoiceOutcomeHandler:
    def __init__(self, services: VoiceOutcomeServices) -> None:
        self._client = services.client
        self._voice = services.voice
        self._material = services.material
        self._max_seconds = services.max_seconds

    async def submit(self, request: VoiceSubmission) -> None:
        outcome, transcript = await self._voice.submit(request)
        if transcript:
            return
        if outcome is MediaOutcome.REFUSED_BY_PLAN:
            await self._client.send_message(SendMessage(request.chat_id, VOICE_PLAN_NOTICE))
            if request.message.body is not None:
                await self._material.plain(
                    request.message,
                    request.user_id,
                    request.chat_id,
                )
        elif outcome is MediaOutcome.TOO_SHORT:
            await self._client.send_message(SendMessage(request.chat_id, VOICE_TOO_SHORT_NOTICE))
        elif outcome is MediaOutcome.TOO_LONG:
            minutes = int(self._max_seconds // 60)
            await self._client.send_message(
                SendMessage(request.chat_id, VOICE_TOO_LONG_TEMPLATE.format(limit=minutes))
            )
        elif outcome is MediaOutcome.UNAVAILABLE:
            await self._material.without_vision(
                request.message,
                request.user_id,
                request.chat_id,
            )
        else:
            await self._client.send_message(SendMessage(request.chat_id, VOICE_EMPTY_NOTICE))
