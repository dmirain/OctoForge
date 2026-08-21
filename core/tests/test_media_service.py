"""The media service: the plan check, the model call and the ledger, in order.

These tests exist because the three used to live apart — the surface called
the model and the core knew nothing about it — and on the deployment where
the surface could not resolve a person, the check and the ledger silently
dropped out. Every test here pins one of the things that made that possible.
"""

import pytest

from octoforge_core.media.api import (
    AudioDuration,
    MediaOutcome,
    TranscriptionRequest,
    none_understood,
    refused_by_plan,
)
from octoforge_core.media.prompt import INGESTION_PROMPT
from octoforge_core.media.service import AudioUnderstanding, ImageUnderstanding, MediaService
from octoforge_core.speech.api import AudioData, TranscriptionUnavailableError
from octoforge_core.tariffs.api import FeatureCode, LimitVerdict, UsageEvent, UsageKind
from octoforge_core.vision.api import ImageData, VisionUnavailableError

PERSON = "person-1"
IMAGE_REF = "tgfile:img-1"
AUDIO_REF = "tgfile:voice-1"
DESCRIPTION = "a cat on a windowsill"
TRANSCRIPT = "привет, как дела"
MIN_SECONDS = 1
MAX_SECONDS = 600.0
RECORDING_SECONDS = 12
TWO = 2
THREE = 3


class RecordingGate:
    """LimitGate stub: configurable features, every usage event kept."""

    def __init__(self, denied: frozenset[str] = frozenset()) -> None:
        self.denied = denied
        self.events: list[UsageEvent] = []
        self.asked: list[tuple[str, str]] = []

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        return None

    async def allows(self, user_id: str, feature: str) -> bool:
        self.asked.append((user_id, feature))
        return feature not in self.denied

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        return LimitVerdict.ok()

    async def max_cron_jobs(self, user_id: str) -> int | None:
        return None

    async def max_datasets(self, user_id: str) -> int | None:
        return None

    async def max_memory_chars(self, user_id: str) -> int | None:
        return None

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


class FakeVision:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        self.prompts.append(prompt)
        return DESCRIPTION


class FakeImages:
    """ImageResolver stub; refs listed in `broken` fail to resolve."""

    def __init__(self, broken: frozenset[str] = frozenset()) -> None:
        self.fetched: list[str] = []
        self._broken = broken

    async def fetch(self, ref: str) -> ImageData:
        if ref in self._broken:
            raise VisionUnavailableError(ref)
        self.fetched.append(ref)
        return ImageData(content=b"bytes")


class FakeSpeech:
    def __init__(self, transcript: str = TRANSCRIPT) -> None:
        self._transcript = transcript

    async def transcribe(self, audio: AudioData) -> str:
        return self._transcript


class FakeAudio:
    def __init__(self, broken: bool = False) -> None:
        self.fetched: list[str] = []
        self._broken = broken

    async def fetch(self, ref: str) -> AudioData:
        if self._broken:
            raise TranscriptionUnavailableError(ref)
        self.fetched.append(ref)
        return AudioData(content=b"bytes")


def make_service(
    gate: RecordingGate | None = None,
    images: ImageUnderstanding | None = None,
    audio: AudioUnderstanding | None = None,
) -> MediaService:
    return MediaService(
        images=images if images is not None else ImageUnderstanding(FakeVision(), FakeImages()),
        audio=audio if audio is not None else AudioUnderstanding(FakeSpeech(), FakeAudio()),
        limits=gate,
    )


def image_understanding(
    resolver: FakeImages | None = None, client: FakeVision | None = None
) -> ImageUnderstanding:
    return ImageUnderstanding(client or FakeVision(), resolver or FakeImages())


def audio_understanding(
    resolver: FakeAudio | None = None, client: FakeSpeech | None = None
) -> AudioUnderstanding:
    return AudioUnderstanding(client or FakeSpeech(), resolver or FakeAudio())


def transcription(seconds: int | None = RECORDING_SECONDS) -> TranscriptionRequest:
    return TranscriptionRequest(PERSON, AUDIO_REF, AudioDuration(seconds, MIN_SECONDS, MAX_SECONDS))


# --- the plan decides before anything is spent -------------------------------


async def test_a_plan_without_vision_costs_nothing() -> None:
    """The check comes before the fetch: a refusal must not download a byte."""
    images = FakeImages()
    gate = RecordingGate(denied=frozenset({FeatureCode.VISION}))
    service = make_service(gate, images=image_understanding(resolver=images))

    results = await service.describe(PERSON, [IMAGE_REF])

    assert refused_by_plan(results)
    assert images.fetched == []
    assert gate.events == []


async def test_a_plan_without_voice_costs_nothing() -> None:
    audio = FakeAudio()
    gate = RecordingGate(denied=frozenset({FeatureCode.VOICE_TRANSCRIPTION}))
    service = make_service(gate, audio=audio_understanding(resolver=audio))

    result = await service.transcribe(transcription())

    assert result.outcome is MediaOutcome.REFUSED_BY_PLAN
    assert audio.fetched == []
    assert gate.events == []


async def test_the_plan_is_answered_before_the_duration() -> None:
    """Ordering with teeth: somebody whose tariff has no voice must hear that,
    not "your recording was too short" — no length would have worked for them,
    and the shorter answer sends them off to re-record for nothing."""
    gate = RecordingGate(denied=frozenset({FeatureCode.VOICE_TRANSCRIPTION}))
    service = make_service(gate)

    too_short = await service.transcribe(transcription(0))
    too_long = await service.transcribe(transcription(9999))

    assert too_short.outcome is MediaOutcome.REFUSED_BY_PLAN
    assert too_long.outcome is MediaOutcome.REFUSED_BY_PLAN


async def test_the_gate_is_asked_once_per_call_not_once_per_image() -> None:
    gate = RecordingGate()
    service = make_service(gate)

    await service.describe(PERSON, [IMAGE_REF, "tgfile:img-2", "tgfile:img-3"])

    assert gate.asked == [(PERSON, FeatureCode.VISION)]


# --- what actually happens when allowed --------------------------------------


async def test_a_described_image_is_ledgered_once_per_success() -> None:
    gate = RecordingGate()
    vision = FakeVision()
    service = make_service(gate, images=image_understanding(client=vision))

    results = await service.describe(PERSON, [IMAGE_REF, "tgfile:img-2"])

    assert [item.text for item in results] == [DESCRIPTION, DESCRIPTION]
    assert vision.prompts == [INGESTION_PROMPT, INGESTION_PROMPT]
    (event,) = gate.events
    assert event.kind is UsageKind.VISION
    assert event.quantity == TWO
    assert event.user_id == PERSON


async def test_only_the_images_that_worked_are_ledgered() -> None:
    """A fetch that failed cost the provider nothing and must not read as spend."""
    gate = RecordingGate()
    service = make_service(
        gate, images=image_understanding(FakeImages(broken=frozenset({"tgfile:img-2"})))
    )

    results = await service.describe(PERSON, [IMAGE_REF, "tgfile:img-2", "tgfile:img-3"])

    assert [item.outcome for item in results] == [
        MediaOutcome.OK,
        MediaOutcome.UNAVAILABLE,
        MediaOutcome.OK,
    ]
    (event,) = gate.events
    assert event.quantity == TWO  # three asked, two described


async def test_a_transcript_is_ledgered_in_seconds() -> None:
    gate = RecordingGate()
    service = make_service(gate)

    result = await service.transcribe(transcription())

    assert result.outcome is MediaOutcome.OK
    assert result.text == TRANSCRIPT
    (event,) = gate.events
    assert event.kind is UsageKind.VOICE_TRANSCRIPTION
    assert event.quantity == RECORDING_SECONDS


async def test_silence_is_a_successful_transcript_of_nothing() -> None:
    """The surface says "I heard nothing" in its own words; this is not a failure."""
    service = make_service(
        RecordingGate(), audio=audio_understanding(client=FakeSpeech(transcript=""))
    )

    result = await service.transcribe(transcription())

    assert result.outcome is MediaOutcome.OK
    assert result.text == ""


# --- duration bounds ---------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, MediaOutcome.TOO_SHORT),
        (601, MediaOutcome.TOO_LONG),
        (RECORDING_SECONDS, MediaOutcome.OK),
    ],
)
async def test_duration_bounds_are_applied_for_an_allowed_plan(
    seconds: int, expected: MediaOutcome
) -> None:
    service = make_service(RecordingGate())

    result = await service.transcribe(transcription(seconds))

    assert result.outcome is expected


async def test_an_unknown_duration_skips_the_bounds() -> None:
    """A document-audio upload declares no length; there is nothing to compare."""
    gate = RecordingGate()
    service = make_service(gate)

    result = await service.transcribe(transcription(None))

    assert result.outcome is MediaOutcome.OK
    assert gate.events == []  # nothing to meter: seconds unknown, so no seconds billed


# --- failures and absence ----------------------------------------------------


async def test_a_failed_transcription_is_unavailable_not_an_exception() -> None:
    """A crash here would take down the message that carried the recording."""
    gate = RecordingGate()
    service = make_service(gate, audio=audio_understanding(FakeAudio(broken=True)))

    result = await service.transcribe(transcription())

    assert result.outcome is MediaOutcome.UNAVAILABLE
    assert gate.events == []


async def test_unconfigured_media_is_unavailable_for_everyone() -> None:
    """No model configured is a feature that is off, not a plan refusal."""
    service = MediaService(limits=RecordingGate())

    described = await service.describe(PERSON, [IMAGE_REF])
    transcribed = await service.transcribe(transcription())

    assert described[0].outcome is MediaOutcome.UNAVAILABLE
    assert transcribed.outcome is MediaOutcome.UNAVAILABLE
    assert not service.describes_images
    assert not service.transcribes_audio


async def test_no_gate_means_no_restriction_and_no_ledger() -> None:
    """An installation with no tariffs behaves exactly as it did before them."""
    service = make_service(gate=None)

    results = await service.describe(PERSON, [IMAGE_REF])

    assert results[0].outcome is MediaOutcome.OK


# --- the helpers the surface branches on -------------------------------------


async def test_the_classifiers_separate_a_refusal_from_a_failure() -> None:
    """The distinction the caller cannot skip: a plan refusal is spoken out
    loud, a technical failure falls back to text in silence."""
    gate = RecordingGate(denied=frozenset({FeatureCode.VISION}))
    refused = await make_service(gate).describe(PERSON, [IMAGE_REF])
    broken = await make_service(
        RecordingGate(), images=image_understanding(FakeImages(broken=frozenset({IMAGE_REF})))
    ).describe(PERSON, [IMAGE_REF])
    mixed = await make_service(
        RecordingGate(),
        images=image_understanding(FakeImages(broken=frozenset({"tgfile:img-2"}))),
    ).describe(PERSON, [IMAGE_REF, "tgfile:img-2"])

    assert refused_by_plan(refused) and none_understood(refused)
    assert none_understood(broken) and not refused_by_plan(broken)
    assert not none_understood(mixed) and not refused_by_plan(mixed)
