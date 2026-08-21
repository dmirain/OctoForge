"""Orchestration of classic and delegated stored endpoint calls."""

from typing import Any

from octoforge_core.instructions.api import InstructionType
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external_collection import CollectionPourer
from octoforge_core.net.external_contract import content_kind, pagination_seed
from octoforge_core.net.external_flow_types import (
    CollectionCall,
    DelegateCall,
    PourCall,
    SpillRequest,
)
from octoforge_core.net.external_http import ExternalHttpRunner
from octoforge_core.net.external_messages import MAX_BODY_BYTES
from octoforge_core.net.external_pagination import EndpointCollector
from octoforge_core.net.external_prepare import ExternalCallPreparer
from octoforge_core.net.external_response import ResponsePresenter
from octoforge_core.net.external_types import (
    ExternalCallConfig,
    ExternalCallContext,
    ExternalCallResult,
    ExternalCallServices,
    ExternalPage,
    KindCallRequest,
)
from octoforge_core.net.external_validation import validate_params
from octoforge_core.net.spec_types import ToolSpec
from octoforge_core.net.tool_spec import parse_tool_spec

DEFAULT_EXTERNAL_CALL_CONFIG = ExternalCallConfig()
DEFAULT_CALL_CONTEXT = ExternalCallContext()


class ExternalCallExecutor:
    """Resolve one endpoint record and execute it behind the egress policy."""

    def __init__(
        self,
        services: ExternalCallServices,
        config: ExternalCallConfig = DEFAULT_EXTERNAL_CALL_CONFIG,
    ) -> None:
        self._instructions = services.instructions
        self._delegates = dict(config.delegates or {})
        self._preparer = ExternalCallPreparer(services.guard, config.credentials)
        wire_limit = config.spill.wire_limit_bytes if config.spill is not None else MAX_BODY_BYTES
        self._http = ExternalHttpRunner(services.http, config.timeout_seconds, wire_limit)
        self._presenter = ResponsePresenter(config.spill, config.truncate_chars)
        self._pourer = CollectionPourer(config.spill)
        self._collector = EndpointCollector(config.spill, self._load_page)

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        context: ExternalCallContext = DEFAULT_CALL_CONTEXT,
    ) -> ExternalCallResult:
        instruction = await self._instructions.get_by_name(
            name,
            InstructionType.ENDPOINT,
            user_id=context.user_id,
        )
        kind = content_kind(instruction.content)
        if kind is not None:
            return await self._delegate(
                DelegateCall(name, kind, instruction.content, params, context)
            )
        spec = parse_tool_spec(instruction.content)
        call_params = pagination_seed(spec, params, context)
        try:
            validated = validate_params(spec, call_params)
        except ExternalCallError as exc:
            raise ExternalCallError(
                f"{exc}; the endpoint declares this contract: {instruction.content}"
            ) from exc
        collection = CollectionCall(name, spec, validated, context.user_id, context.options)
        if context.options.collect:
            return await self._collector.collect(collection)
        page = await self._load_page(spec, validated, context.user_id)
        if context.options.into is not None:
            return await self._pourer.pour(PourCall(collection, page))
        body = await self._presenter.render(
            SpillRequest(name, context.user_id, context.options.scope, spec.response),
            page,
        )
        return ExternalCallResult(page.status, body)

    async def _delegate(self, call: DelegateCall) -> ExternalCallResult:
        options = call.context.options
        if options.collect or options.into is not None:
            raise ExternalCallError(
                f"endpoint '{call.name}' is a kind record ({call.kind!r}); "
                "collect/into apply to classic HTTP endpoints only"
            )
        delegate = self._delegates.get(call.kind)
        if delegate is None:
            raise ExternalCallError(
                f"endpoint '{call.name}' declares kind {call.kind!r}, which has no executor"
            )
        return await delegate.execute(
            KindCallRequest(call.content, call.params, call.context.user_id, options.scope)
        )

    async def _load_page(
        self,
        spec: ToolSpec,
        validated: dict[str, str],
        user_id: str | None,
    ) -> ExternalPage:
        return await self._http.run(await self._preparer.prepare(spec, validated, user_id))
