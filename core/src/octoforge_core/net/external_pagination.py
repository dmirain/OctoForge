"""Deterministic endpoint pagination into one collection."""

from http import HTTPStatus

from octoforge_core.net.collections.api import REF_PREFIX, CollectionQuotaError
from octoforge_core.net.collections.ingest import (
    CollectionSink,
    CollectionSinkOptions,
    ParsedBody,
    ResponseSpill,
    render_passport,
)
from octoforge_core.net.external_collection import (
    collection_prerequisites,
    parse_page,
    truncate_page_body,
)
from octoforge_core.net.external_cursor import PageCursor, reached_total
from octoforge_core.net.external_flow_types import (
    CollectionCall,
    CollectionProgress,
    CollectionWalk,
    PageLoader,
    WalkOutcome,
)
from octoforge_core.net.external_types import ExternalCallResult, ExternalPage


class EndpointCollector:
    def __init__(self, spill: ResponseSpill | None, load_page: PageLoader) -> None:
        self._spill = spill
        self._load_page = load_page

    async def collect(self, call: CollectionCall) -> ExternalCallResult:
        pagination, limit = collection_prerequisites(call, self._spill)
        assert self._spill is not None and call.user_id is not None
        sink = CollectionSink(
            CollectionSinkOptions(
                self._spill,
                call.user_id,
                f"endpoint:{call.name}",
                label=call.options.label,
                into=call.options.into.removeprefix(REF_PREFIX) if call.options.into else None,
            )
        )
        outcome = await self._walk(CollectionWalk(call, sink, pagination, limit))
        if outcome.early is not None:
            return outcome.early
        progress = outcome.progress
        passport = await sink.finish(progress.capped)
        if passport is None:
            body = f"collect: '{call.name}' returned no records at all"
        else:
            body = f"{render_passport(passport)}\ncollected {progress.pages} page(s) this call"
        return ExternalCallResult(progress.status, body)

    async def _walk(self, walk: CollectionWalk) -> WalkOutcome:
        progress = CollectionProgress()
        cursor = PageCursor(walk.pagination)
        while progress.pages < walk.limit:
            step = await self._step(walk, cursor, progress)
            if isinstance(step, ExternalCallResult):
                return WalkOutcome(progress, step)
            if step is None:
                break
            progress.pages += 1
            if step.record_truncated:
                progress.capped = True
                break
            if not cursor.advance(step) or reached_total(
                walk.pagination, step, walk.sink.record_count
            ):
                break
        else:
            progress.capped = True
        return WalkOutcome(progress)

    async def _step(
        self,
        walk: CollectionWalk,
        cursor: PageCursor,
        progress: CollectionProgress,
    ) -> ParsedBody | ExternalCallResult | None:
        params = {**walk.call.validated, walk.pagination.param: cursor.value}
        page = await self._load_page(walk.call.spec, params, walk.call.user_id)
        progress.status = page.status
        if page.status >= HTTPStatus.BAD_REQUEST:
            if progress.pages == 0:
                return ExternalCallResult(page.status, truncate_page_body(page.body))
            progress.capped = True
            return None
        parsed, capped = await self._store(walk.sink, walk.call, page)
        progress.capped = capped
        return parsed

    @staticmethod
    async def _store(
        sink: CollectionSink,
        call: CollectionCall,
        page: ExternalPage,
    ) -> tuple[ParsedBody | None, bool]:
        try:
            parsed = await parse_page(page, call.spec)
            if parsed is not None:
                await sink.add(parsed, len(page.body))
        except CollectionQuotaError:
            return None, True
        return parsed, False
