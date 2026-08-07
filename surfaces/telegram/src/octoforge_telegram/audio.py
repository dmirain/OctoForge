"""Telegram-backed `AudioResolver`: turns an attachment ref back into bytes.

The transport adapter behind the `AudioResolver` port
(`octoforge_core.speech.api`), and the twin of `images.py`. A voice message
enters the dialog as a transcript, and the transcribing happens in the core
next to the plan check — so the core holds a `tgfile:<file_id>` reference and
this is what turns it back into a recording through the Bot API. Telegram
keeps a bot's files indefinitely, so the file id alone is enough.
"""

import logging

from octoforge_core.speech.api import AudioData
from octoforge_core.speech.api import TranscriptionUnavailableError as AudioUnavailableError

from octoforge_telegram.client import TelegramApiError, TelegramClient
from octoforge_telegram.images import LEGACY_REF_PREFIX, REF_PREFIX

logger = logging.getLogger(__name__)

# Telegram voice notes arrive as Ogg/Opus under an `.oga` name the
# transcription APIs refuse; `speech.api.upload_name` maps it, so what
# matters here is only that a name with an extension travels with the bytes.
DEFAULT_FILE_NAME = "voice.oga"
DEFAULT_MEDIA_TYPE = "audio/ogg"
UNKNOWN_REF_ERROR = "not a Telegram audio ref: {ref!r}"
FETCH_FAILED_ERROR = "could not fetch recording: {error}"


class TelegramAudioResolver:
    """Resolves a Telegram-origin attachment ref (`tgfile:<file_id>`) to bytes.

    Any Bot API failure becomes `TranscriptionUnavailableError`: the speech
    port must never leak `TelegramApiError`, a transport-specific type, into
    core-facing code.
    """

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def fetch(self, ref: str) -> AudioData:
        """Resolve `ref` to audio bytes; raises on an unknown ref or any failure."""
        file_id = _file_id(ref)
        if file_id is None:
            raise AudioUnavailableError(UNKNOWN_REF_ERROR.format(ref=ref))
        try:
            file_path = await self._client.get_file(file_id)
            content = await self._client.download_file(file_path)
        except TelegramApiError as exc:
            logger.warning("could not resolve audio ref %s: %s", ref, exc)
            raise AudioUnavailableError(FETCH_FAILED_ERROR.format(error=exc)) from exc
        return AudioData(
            content=content, file_name=_file_name(file_path), media_type=DEFAULT_MEDIA_TYPE
        )


def _file_name(file_path: str) -> str:
    """The name to upload under: the resolved path's own, or a safe default.

    The extension is what these APIs pick a decoder by, so the resolved path
    (`voice/file_42.oga`) carries more truth than any constant would.
    """
    tail = file_path.rsplit("/", 1)[-1]
    return tail if "." in tail else DEFAULT_FILE_NAME


def _file_id(ref: str) -> str | None:
    """Extract the Bot API file id from a ref, or None when it is not ours.

    Accepts the legacy prefix for the same reason `images.py` does: refs
    written before the rename live in the message history forever.
    """
    for prefix in (REF_PREFIX, LEGACY_REF_PREFIX):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return None
