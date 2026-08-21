"""Append one endpoint page to an existing collection."""

from octoforge_core.net.collections.api import REF_PREFIX, CollectionQuotaError
from octoforge_core.net.collections.ingest import (
    CollectionSink,
    CollectionSinkOptions,
    ParsedBody,
    ResponseSpill,
    parse_structured,
    render_passport,
    shape_records,
)
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external_flow_types import CollectionCall, PourCall
from octoforge_core.net.external_messages import TRUNCATED_SUFFIX
from octoforge_core.net.external_types import ExternalCallResult, ExternalPage
from octoforge_core.net.spec_types import PaginationSpec, ToolSpec

COLLECT_UNAVAILABLE_MESSAGE = (
    "collections are not available here (they need Postgres and a user in context); "
    "call the endpoint without collect/into"
)


def truncate_page_body(body: str) -> str:
    limit = 8000
    return body if len(body) <= limit else body[:limit] + TRUNCATED_SUFFIX


def collection_prerequisites(
    call: CollectionCall,
    spill: ResponseSpill | None,
) -> tuple[PaginationSpec, int]:
    pagination = call.spec.pagination
    if pagination is None:
        raise ExternalCallError(
            f"endpoint '{call.name}' declares no pagination section; collect needs one "
            "(add it to the record: kind page/offset/cursor, the param to advance)"
        )
    if spill is None or spill.store is None or call.user_id is None:
        raise ExternalCallError(COLLECT_UNAVAILABLE_MESSAGE)
    ceiling = spill.config.collect_max_pages
    return pagination, min(call.options.max_pages or ceiling, ceiling)


async def parse_page(page: ExternalPage, spec: ToolSpec) -> ParsedBody | None:
    path = spec.response.items_path if spec.response is not None else None
    parsed = await parse_structured(page.body, page.content_type, path)
    if parsed is None or not parsed.records.payloads:
        return None
    if spec.response is not None and spec.response.fields:
        return ParsedBody(
            parsed.kind,
            shape_records(parsed.records, spec.response.fields),
            parsed.envelope,
            parsed.document,
            parsed.single_document,
            parsed.record_truncated,
        )
    return parsed


class CollectionPourer:
    def __init__(self, spill: ResponseSpill | None) -> None:
        self._spill = spill

    async def pour(self, call: PourCall) -> ExternalCallResult:
        request = call.request
        options = request.options
        if self._spill is None or self._spill.store is None or request.user_id is None:
            raise ExternalCallError(COLLECT_UNAVAILABLE_MESSAGE)
        parsed = await parse_page(call.page, request.spec)
        if parsed is None:
            raise ExternalCallError(
                f"the response of '{request.name}' carried no records to add "
                f"(HTTP {call.page.status}); nothing was appended"
            )
        assert options.into is not None
        sink = CollectionSink(
            CollectionSinkOptions(
                self._spill,
                request.user_id,
                f"endpoint:{request.name}",
                into=options.into.removeprefix(REF_PREFIX),
            )
        )
        try:
            await sink.add(parsed, len(call.page.body))
        except CollectionQuotaError as exc:
            raise ExternalCallError(
                f"nothing appended: {exc}; drop an old collection or query what is there"
            ) from exc
        passport = await sink.finish(call.page.wire_truncated or parsed.record_truncated)
        assert passport is not None
        return ExternalCallResult(call.page.status, render_passport(passport))
