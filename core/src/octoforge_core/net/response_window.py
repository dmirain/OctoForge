"""Position-based window reading for remembered responses."""

from typing import Any

from octoforge_core.net.response_models import NOT_FOUND_TEMPLATE, ResponseNotFoundError
from octoforge_core.net.response_tool_specs import (
    WINDOW_DEFAULT_AFTER,
    WINDOW_DEFAULT_BEFORE,
    WINDOW_DESCRIPTION,
    WINDOW_NAME,
    WINDOW_SCHEMA,
)
from octoforge_core.net.response_tool_support import (
    MemoryToolBase,
    PositiveRule,
    parse_optional_str,
    parse_positive,
    parse_ref,
)
from octoforge_core.tariffs.api import FeatureCode, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class ResponseWindowTool(MemoryToolBase):
    """Read a wider window around a position returned by response_find."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=WINDOW_NAME, description=WINDOW_DESCRIPTION, parameters_schema=WINDOW_SCHEMA
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = parse_ref(arguments.get("ref"))
        at = arguments.get("at")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ToolArgumentsError("at must be a position from a response_find match")
        key = parse_optional_str(arguments.get("key"), "key")
        before = parse_positive(
            arguments.get("before"), PositiveRule("before", WINDOW_DEFAULT_BEFORE)
        )
        after = parse_positive(arguments.get("after"), PositiveRule("after", WINDOW_DEFAULT_AFTER))
        try:
            item = await self._home.fetch(context.user_id, ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, key)
        if at >= len(target):
            return f"position {at} is past the end ({len(target)} chars)"
        start, end = max(0, at - before), min(len(target), at + after)
        return f"[{start}-{end} of {len(target)}] …{target[start:end]}…"
