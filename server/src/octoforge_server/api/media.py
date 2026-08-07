"""Media endpoints: the way an out-of-process surface reaches the model.

Ingestion may run as its own node — one process must own the Telegram long
poll, so it moves out of the pods. That node knows accounts, not people, and
holds no core database, which is exactly why it must not call a vision or
speech model itself: the plan check and the usage ledger are keyed by person
and it cannot key anything.

So it asks here instead. `get_user_id` resolves the account into a person and
admits them (a banned account cannot burn a vision call), and the service
does the rest in `MediaService` — check, call, ledger.

What crosses the wire is **references**, not bytes: `AttachmentSpec.ref` is
already how a file-like thing travels this API, and the service resolves them
through the same Bot API client that answers `image_look`.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from octoforge_core.media.api import MediaOutcome, MediaResult
from octoforge_core.media.service import MediaService
from pydantic import BaseModel, Field

from octoforge_server.deps import get_media_service, get_user_id

router = APIRouter(prefix="/api/media")

MAX_REFS = 16

MediaDep = Annotated[MediaService, Depends(get_media_service)]
UserIdDep = Annotated[str, Depends(get_user_id)]


class DescribeRequest(BaseModel):
    """The pictures of one message, in the order they were sent."""

    refs: list[str] = Field(min_length=1, max_length=MAX_REFS)


class TranscribeRequest(BaseModel):
    """One recording, with what the transport knows and enforces about it."""

    ref: str
    #: What the transport declared; null when it did not know (an audio
    #: document), in which case the bounds below do not apply.
    seconds: int | None = None
    min_seconds: int = 0
    max_seconds: float = 0.0


class MediaResultResponse(BaseModel):
    """One outcome, and the text when there is any."""

    outcome: MediaOutcome
    text: str = ""

    @classmethod
    def of(cls, result: MediaResult) -> "MediaResultResponse":
        return cls(outcome=result.outcome, text=result.text)


class DescribeResponse(BaseModel):
    """One result per reference, in the order they were asked about."""

    results: list[MediaResultResponse]


class CapabilitiesResponse(BaseModel):
    """What this service can actually do with media.

    The ingestion node asks once at startup and logs the answer. Its own
    settings say nothing about this any more — the models are configured
    here — and a node that could not report what it is about to rely on is
    how images and voice once degraded silently for a day.
    """

    describes_images: bool
    transcribes_audio: bool


@router.post("/describe", status_code=HTTPStatus.OK)
async def describe(
    request: DescribeRequest, user_id: UserIdDep, media: MediaDep
) -> DescribeResponse:
    """Describe each picture; one result per reference, failures included."""
    results = await media.describe(user_id, request.refs)
    return DescribeResponse(results=[MediaResultResponse.of(item) for item in results])


@router.post("/transcribe", status_code=HTTPStatus.OK)
async def transcribe(
    request: TranscribeRequest, user_id: UserIdDep, media: MediaDep
) -> MediaResultResponse:
    """Transcribe one recording; the plan is answered before the duration."""
    return MediaResultResponse.of(
        await media.transcribe(
            user_id, request.ref, request.seconds, request.min_seconds, request.max_seconds
        )
    )


@router.get("/capabilities")
async def capabilities(media: MediaDep) -> CapabilitiesResponse:
    """What media this installation understands (no person involved)."""
    return CapabilitiesResponse(
        describes_images=media.describes_images, transcribes_audio=media.transcribes_audio
    )
