"""Telegram-backed `ImageResolver`: turns an attachment ref back into bytes.

This is the transport adapter behind the `ImageResolver` port
(`octoforge_core.vision.api`): the strong vision tier's `image_look` tool
asks for the dialog's most recent image by its `Attachment.ref`, and this
class is what turns that ref back into bytes via the Bot API. Telegram keeps
a bot's files indefinitely, so the file id alone is enough — nothing is
stored locally.
"""

import logging

from octoforge_core.vision.api import ImageData, VisionUnavailableError

from octoforge_web.telegram.client import TelegramApiError, TelegramClient

logger = logging.getLogger(__name__)

# The ref prefix an `Attachment` of Telegram origin carries (the sole producer
# is `poller.py`); a ref lacking it belongs to a different transport and must
# never be treated as ours.
#
# `tgfile:` rather than `tg:` because `tg:` is already the prefix of a Telegram
# USER ID (`client.py:USER_ID_PREFIX`). Two different kinds of identifier
# sharing one namespace is a bug waiting for the first piece of code that
# checks the prefix to decide what it is holding.
REF_PREFIX = "tgfile:"
# Refs written before the rename are in the message history forever, so reads
# still accept the old prefix. Writes never use it.
LEGACY_REF_PREFIX = "tg:"
# Telegram photos are always transcoded to JPEG by the Bot API; documents
# keep their declared MIME type at ingestion, but that type does not travel
# with the ref, so a cheap extension check on the resolved file path is the
# best available signal for anything else
DEFAULT_MEDIA_TYPE = "image/jpeg"
_EXTENSION_MEDIA_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
}
UNKNOWN_REF_ERROR = "not a Telegram image ref: {ref!r}"
FETCH_FAILED_ERROR = "could not fetch image: {error}"


class TelegramImageResolver:
    """Resolves a Telegram-origin attachment ref (`tgfile:<file_id>`) to bytes.

    A ref with a foreign prefix, an unknown file, or any Bot API failure
    turns into `VisionUnavailableError`: the vision port must never leak
    `TelegramApiError`, a transport-specific type, into tool-facing code.
    """

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def fetch(self, ref: str) -> ImageData:
        """Resolve `ref` to image bytes; raises `VisionUnavailableError` on any failure."""
        file_id = _file_id(ref)
        if file_id is None:
            raise VisionUnavailableError(UNKNOWN_REF_ERROR.format(ref=ref))
        try:
            file_path = await self._client.get_file(file_id)
            content = await self._client.download_file(file_path)
        except TelegramApiError as exc:
            logger.warning("could not resolve image ref %s: %s", ref, exc)
            raise VisionUnavailableError(FETCH_FAILED_ERROR.format(error=exc)) from exc
        return ImageData(content=content, media_type=_media_type(file_path))


def _media_type(file_path: str) -> str:
    """Guess the media type from the resolved file path's extension."""
    lowered = file_path.lower()
    for suffix, media_type in _EXTENSION_MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media_type
    return DEFAULT_MEDIA_TYPE


def _file_id(ref: str) -> str | None:
    """Extract the Bot API file id from a ref, or None when it is not ours.

    Accepts the legacy prefix as well: attachments stored before the rename
    live in the message history indefinitely, and a picture sent last month
    must still resolve.
    """
    for prefix in (REF_PREFIX, LEGACY_REF_PREFIX):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return None
