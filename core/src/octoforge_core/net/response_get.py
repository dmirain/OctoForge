"""The deliberate whole-response or JSON-key reading tool."""

from typing import Any

from octoforge_core.net.response_models import NOT_FOUND_TEMPLATE, ResponseNotFoundError
from octoforge_core.net.response_passport import THOUSAND, estimate_tokens
from octoforge_core.net.response_tool_specs import GET_DESCRIPTION, GET_NAME, GET_SCHEMA
from octoforge_core.net.response_tool_support import (
    MemoryToolBase,
    PositiveRule,
    parse_optional_str,
    parse_positive,
    parse_ref,
)
from octoforge_core.tariffs.api import FeatureCode, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec


class ResponseGetTool(MemoryToolBase):
    """Read a deliberately budgeted response or JSON key."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=GET_NAME, description=GET_DESCRIPTION, parameters_schema=GET_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = parse_ref(arguments.get("ref"))
        key = parse_optional_str(arguments.get("key"), "key")
        rule = PositiveRule("max_chars", self._config.get_default_chars)
        cap = min(parse_positive(arguments.get("max_chars"), rule), self._config.get_max_chars)
        try:
            item = await self._home.fetch(context.user_id, ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, key)
        if len(target) <= cap:
            return target
        tokens = _human_tokens(estimate_tokens(target))
        return (
            f"{target[:cap]}\n…[showing {cap} of {len(target)} chars ({tokens} total); "
            f"raise max_chars (ceiling {self._config.get_max_chars}) or search with response_find]"
        )


def _human_tokens(count: int) -> str:
    return f"~{count / THOUSAND:.1f}k tokens" if count >= THOUSAND else f"~{count} tokens"
