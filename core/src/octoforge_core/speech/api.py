"""Public boundary of the speech module: hearing what the user said.

The main model reads text, so a voice message is turned into text before it
ever reaches the dialog — by a SEPARATE model behind this port, exactly like
images are (`octoforge_core.vision.api`). One tier is enough here: unlike a
picture, a recording has no "look closer" — the transcript IS the message.

The asymmetry with pictures that matters downstream: a bare photo is
material (the user shared something without asking anything), while a voice
message the user recorded is them speaking. That decision belongs to the
transport, which knows who recorded what; this port only produces text.
"""

from dataclasses import dataclass
from typing import Protocol

# what the OpenAI-compatible transcription endpoints accept as a file name
# extension. The check is on the NAME, not the content: Telegram hands out
# voice notes as `.oga` (a legitimate Ogg extension) and the API rejects it
# outright, so a name has to be normalized before the upload.
ACCEPTED_EXTENSIONS = frozenset(
    {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".opus", ".wav", ".webm"}
)
EXTENSION_ALIASES = {".oga": ".ogg"}
DEFAULT_UPLOAD_NAME = "audio.ogg"


@dataclass(frozen=True, slots=True)
class AudioData:
    """Raw audio bytes with the file name the transcription endpoint needs.

    `file_name` is not decoration: these APIs pick the decoder by extension,
    so it travels with the bytes from whoever downloaded them.
    """

    content: bytes
    file_name: str = DEFAULT_UPLOAD_NAME
    media_type: str = "audio/ogg"


class TranscriptionClient(Protocol):
    """Turns spoken audio into text; one call, one transcript."""

    async def transcribe(self, audio: AudioData) -> str:
        """Transcribe the recording; returns the empty string for silence."""
        ...


class TranscriptionUnavailableError(Exception):
    """Speech-to-text is not configured, or the recording cannot be read."""


def upload_name(file_name: str) -> str:
    """The file name to upload under: an extension the endpoint accepts.

    Keeps an already-accepted extension, maps a known alias (`.oga` → `.ogg`,
    the same Ogg container under a name the API refuses), and falls back to a
    plain default for anything unrecognized — a wrong guess only costs one
    rejected request, while sending `.oga` verbatim costs every voice message.
    """
    suffix = file_name[file_name.rfind(".") :].lower() if "." in file_name else ""
    if suffix in ACCEPTED_EXTENSIONS:
        return file_name
    alias = EXTENSION_ALIASES.get(suffix)
    if alias is not None:
        return f"{file_name[: file_name.rfind('.')]}{alias}"
    return DEFAULT_UPLOAD_NAME
