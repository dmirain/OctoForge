"""`MediaUnderstanding` over HTTP: how the ingestion node reaches the core.

The twin of `ApiGatewayRegistry` in `gateway.py`, and there for the same
reason: when ingestion is its own process it holds no core database, so
anything keyed by a person — the plan check, the usage ledger — has to happen
on the service. It sends the reference and the service does the rest.

A failure to reach the service is not an exception here but an `UNAVAILABLE`
outcome, because that is what the caller already knows how to handle: the
picture keeps its slot with a placeholder, or the message falls back to the
text-only path it had before vision existed. A raise would cost the user the
message itself.
"""

import logging
from collections.abc import Sequence

import httpx
from octoforge_core.media.api import MediaOutcome, MediaResult

from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX
from octoforge_telegram.gateway import CHANNEL_HEADER, USER_ID_HEADER

logger = logging.getLogger(__name__)

DESCRIBE_PATH = "/api/media/describe"
TRANSCRIBE_PATH = "/api/media/transcribe"
CAPABILITIES_PATH = "/api/media/capabilities"
# describing a dozen pictures takes as long as a dozen model calls; the
# default client timeout is nowhere near that
MEDIA_TIMEOUT_SECONDS = 300.0


class ApiMediaUnderstanding:
    """Asks the service to understand media on a Telegram account's behalf."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def describe(self, user_id: str, refs: Sequence[str]) -> tuple[MediaResult, ...]:
        body = await self._post(DESCRIBE_PATH, user_id, {"refs": list(refs)})
        listed = body.get("results") if body is not None else None
        if not isinstance(listed, list):
            return tuple(MediaResult(MediaOutcome.UNAVAILABLE) for _ in refs)
        results = [_result_of(item) for item in listed]
        if len(results) != len(refs):
            logger.warning(
                "media describe answered %d results for %d refs", len(results), len(refs)
            )
            return tuple(MediaResult(MediaOutcome.UNAVAILABLE) for _ in refs)
        return tuple(results)

    async def transcribe(
        self,
        user_id: str,
        ref: str,
        seconds: int | None,
        min_seconds: int,
        max_seconds: float,
    ) -> MediaResult:
        body = await self._post(
            TRANSCRIBE_PATH,
            user_id,
            {
                "ref": ref,
                "seconds": seconds,
                "min_seconds": min_seconds,
                "max_seconds": max_seconds,
            },
        )
        if body is None:
            return MediaResult(MediaOutcome.UNAVAILABLE)
        return _result_of(body)

    async def capabilities(self) -> dict[str, bool] | None:
        """What the service says it can do; None when it could not be asked.

        Read once at startup and logged. This node's own settings say nothing
        about vision or speech any more — the models are configured on the
        service — and a node that cannot report what it is about to depend on
        is how images and voice once degraded silently for a whole day.
        """
        try:
            response = await self._client.get(CAPABILITIES_PATH, timeout=MEDIA_TIMEOUT_SECONDS)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            logger.warning("could not ask the service what media it understands: %s", exc)
            return None
        return {key: bool(value) for key, value in body.items()}

    async def _post(
        self, path: str, user_id: str, body: dict[str, object]
    ) -> dict[str, object] | None:
        """One call; None means the service could not answer at all."""
        try:
            response = await self._client.post(
                path,
                json=body,
                headers={
                    USER_ID_HEADER: user_id.removeprefix(USER_ID_PREFIX),
                    CHANNEL_HEADER: TELEGRAM_CHANNEL,
                },
                timeout=MEDIA_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            parsed = response.json()
        except Exception as exc:
            logger.warning("media call %s failed: %s", path, exc)
            return None
        return parsed if isinstance(parsed, dict) else None


def _result_of(payload: object) -> MediaResult:
    """One result from the wire; anything unrecognized reads as unavailable."""
    if not isinstance(payload, dict):
        return MediaResult(MediaOutcome.UNAVAILABLE)
    try:
        outcome = MediaOutcome(str(payload.get("outcome")))
    except ValueError:
        logger.warning("media call returned an unknown outcome %r", payload.get("outcome"))
        return MediaResult(MediaOutcome.UNAVAILABLE)
    return MediaResult(outcome, str(payload.get("text", "")))
