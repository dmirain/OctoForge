"""Literal search with merged context windows over remembered responses."""

import asyncio
from dataclasses import dataclass
from typing import Any

from octoforge_core.net.response_models import NOT_FOUND_TEMPLATE, ResponseNotFoundError
from octoforge_core.net.response_search import find_positions, merge_windows
from octoforge_core.net.response_tool_specs import (
    FIND_DEFAULT_AFTER,
    FIND_DEFAULT_BEFORE,
    FIND_DEFAULT_MATCHES,
    FIND_DESCRIPTION,
    FIND_MAX_MATCHES,
    FIND_NAME,
    FIND_SCHEMA,
    MAX_PATTERN_CHARS,
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


@dataclass(frozen=True, slots=True)
class _FindOptions:
    ref: str
    pattern: str
    key: str | None
    before: int
    after: int
    max_matches: int
    offset: int


class ResponseFindTool(MemoryToolBase):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=FIND_NAME, description=FIND_DESCRIPTION, parameters_schema=FIND_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        options = _parse_options(arguments)
        try:
            item = await self._home.fetch(context.user_id, options.ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, options.key)
        positions = await asyncio.to_thread(find_positions, target, options.pattern)
        if not positions:
            return f"no matches for {options.pattern!r} in {len(target)} chars"
        return _render_matches(target, positions, options)


def _parse_options(arguments: dict[str, Any]) -> _FindOptions:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolArgumentsError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ToolArgumentsError(
            f"pattern is longer than {MAX_PATTERN_CHARS} chars; search for a "
            "shorter distinctive fragment instead"
        )
    return _FindOptions(
        ref=parse_ref(arguments.get("ref")),
        pattern=pattern,
        key=parse_optional_str(arguments.get("key"), "key"),
        before=parse_positive(arguments.get("before"), PositiveRule("before", FIND_DEFAULT_BEFORE)),
        after=parse_positive(arguments.get("after"), PositiveRule("after", FIND_DEFAULT_AFTER)),
        max_matches=min(
            parse_positive(
                arguments.get("max_matches"), PositiveRule("max_matches", FIND_DEFAULT_MATCHES)
            ),
            FIND_MAX_MATCHES,
        ),
        offset=parse_positive(arguments.get("match_offset"), PositiveRule("match_offset", 0, 0)),
    )


def _render_matches(target: str, positions: list[tuple[int, int]], options: _FindOptions) -> str:
    shown = positions[options.offset : options.offset + options.max_matches]
    bounds = [
        (max(0, at - options.before), min(len(target), end + options.after), at)
        for at, end in shown
    ]
    blocks = [f"[at={at}] …{target[start:end]}…" for start, end, at in merge_windows(bounds)]
    more = " — narrow the pattern or page with match_offset"
    summary = (
        f"{len(positions)} match(es); showing {options.offset + 1}-{options.offset + len(shown)}"
    )
    if len(positions) > len(shown):
        summary += more
    return summary + "\n" + "\n---\n".join(blocks)
