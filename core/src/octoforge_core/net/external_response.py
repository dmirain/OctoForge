"""Present one endpoint response through spill or bounded inline text."""

from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.collections.ingest import SpillRequest as IngestSpillRequest
from octoforge_core.net.external_flow_types import SpillRequest
from octoforge_core.net.external_messages import (
    NO_CREDENTIAL_HINT,
    TRUNCATED_SUFFIX,
    UNAUTHENTICATED_STATUSES,
)
from octoforge_core.net.external_types import ExternalPage


class ResponsePresenter:
    def __init__(self, spill: ResponseSpill | None, truncate_chars: int) -> None:
        self._spill = spill
        self._truncate_chars = truncate_chars

    async def render(self, request: SpillRequest, page: ExternalPage) -> str:
        rendered = await self._spill_body(request, page)
        if rendered is None:
            rendered = self._truncate(page.body)
            if page.wire_truncated and not rendered.endswith(TRUNCATED_SUFFIX):
                rendered += TRUNCATED_SUFFIX
        if page.status in UNAUTHENTICATED_STATUSES and not page.had_secrets:
            rendered += NO_CREDENTIAL_HINT
        return rendered

    def _truncate(self, body: str) -> str:
        if len(body) <= self._truncate_chars:
            return body
        return body[: self._truncate_chars] + TRUNCATED_SUFFIX

    async def _spill_body(self, request: SpillRequest, page: ExternalPage) -> str | None:
        if self._spill is None or request.user_id is None:
            return None
        return await self._spill.spill(
            IngestSpillRequest(
                owner_id=request.user_id,
                body=page.body,
                content_type=page.content_type,
                source=f"endpoint:{request.name}",
                wire_truncated=page.wire_truncated,
                scope=request.scope,
                response=request.response,
            )
        )
