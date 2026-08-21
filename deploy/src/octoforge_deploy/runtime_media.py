"""Media clients and surface-specific reference resolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from octoforge_core.identity.api import IdentityStore
from octoforge_core.media.api import MediaResult, TranscriptionRequest
from octoforge_core.media.service import AudioUnderstanding, ImageUnderstanding, MediaService
from octoforge_core.speech.api import AudioResolver, TranscriptionClient
from octoforge_core.speech.client import OpenAITranscriptionClient
from octoforge_core.tariffs.api import LimitGate
from octoforge_core.vision.api import ImageResolver, VisionClient
from octoforge_core.vision.client import OpenAIVisionClient
from octoforge_server.config import Settings
from octoforge_telegram.audio import TelegramAudioResolver
from octoforge_telegram.client import USER_ID_PREFIX, TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.images import TelegramImageResolver

from octoforge_deploy.runtime_http import HttpClients


@dataclass(frozen=True, slots=True)
class MediaServices:
    service: MediaService
    deep_vision: VisionClient | None
    image_resolver: ImageResolver | None


@dataclass(frozen=True, slots=True)
class MediaBuildRequest:
    settings: Settings
    telegram: TelegramSettings
    limits: LimitGate


class PersonScopedMedia:
    """Resolve a surface account to its person before checking limits."""

    def __init__(self, media: MediaService, identities: IdentityStore, channel: str) -> None:
        self._media = media
        self._identities = identities
        self._channel = channel

    async def describe(self, user_id: str, refs: Sequence[str]) -> tuple[MediaResult, ...]:
        return await self._media.describe(await self._person(user_id), refs)

    async def transcribe(self, request: TranscriptionRequest) -> MediaResult:
        return await self._media.transcribe(
            replace(request, user_id=await self._person(request.user_id)),
        )

    async def _person(self, user_id: str) -> str:
        return await self._identities.resolve_or_create(
            self._channel,
            user_id.removeprefix(USER_ID_PREFIX),
        )


def build_media_services(request: MediaBuildRequest, clients: HttpClients) -> MediaServices:
    cheap = _vision(request.settings, clients, deep=False)
    deep = _vision(request.settings, clients, deep=True)
    speech = _speech(request.settings, clients)
    image_resolver = _image_resolver(request.telegram, clients)
    audio_resolver = _audio_resolver(request.telegram, clients)
    return MediaServices(
        MediaService(
            images=_images(cheap, image_resolver),
            audio=_audio(speech, audio_resolver),
            limits=request.limits,
        ),
        deep,
        image_resolver,
    )


def _vision(settings: Settings, clients: HttpClients, *, deep: bool) -> VisionClient | None:
    configured = settings.deep_vision_configured() if deep else settings.vision_configured()
    if not configured:
        return None
    config = settings.to_deep_vision_config() if deep else settings.to_vision_config()
    return OpenAIVisionClient(clients.vision, config)


def _speech(settings: Settings, clients: HttpClients) -> TranscriptionClient | None:
    if not settings.speech_configured():
        return None
    return OpenAITranscriptionClient(clients.speech, settings.to_speech_config())


def _bot(settings: TelegramSettings, clients: HttpClients) -> TelegramBotClient | None:
    if not settings.telegram_bot_token:
        return None
    return TelegramBotClient(clients.outbound, settings.telegram_bot_token)


def _image_resolver(settings: TelegramSettings, clients: HttpClients) -> ImageResolver | None:
    bot = _bot(settings, clients)
    return TelegramImageResolver(bot) if bot is not None else None


def _audio_resolver(settings: TelegramSettings, clients: HttpClients) -> AudioResolver | None:
    bot = _bot(settings, clients)
    return TelegramAudioResolver(bot) if bot is not None else None


def _images(
    client: VisionClient | None, resolver: ImageResolver | None
) -> ImageUnderstanding | None:
    return None if client is None or resolver is None else ImageUnderstanding(client, resolver)


def _audio(
    client: TranscriptionClient | None,
    resolver: AudioResolver | None,
) -> AudioUnderstanding | None:
    return None if client is None or resolver is None else AudioUnderstanding(client, resolver)
