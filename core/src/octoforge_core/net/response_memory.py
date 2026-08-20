"""Task-scoped response memory: a fetched document parked in RAM, not context.

The database collections (net/collections) hold ARRAYS — records worth
filtering, aggregating and keeping across task boundaries. This module holds
the other shape: a single document (one JSON object, an article, a page)
whose value is to be *read* — fully when it fits the model's budget, by
search when it does not. It lives exactly as long as the task that fetched
it: every run carries a task id, and the runner drops a task's responses the
moment its process terminates. A restart or a dialog migration loses them,
and the remedy is the same as everywhere else in this system — fetch again.

The model decides what enters context, so the passport gives it the numbers
to decide with: per-key sizes in characters AND an honest token estimate
(Cyrillic tokenizes ~2.4 chars/token, Latin ~4 — a plain char cap starves
Russian text twice). Deliberate full reads go through `response_get` with a
caller-chosen `max_chars`; a document too big for any budget is worked by
`response_find` (windows around matches) and `response_window` (a bigger
window around a position a find returned). Sequential window-paging is
deliberately absent: reading a megabyte in slices costs the same tokens as
reading it whole, just slower.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

REF_PREFIX = "resp:"

DEFAULT_MAX_RESPONSE_MB = 8
DEFAULT_BUDGET_MB = 200
DEFAULT_GET_CHARS = 8000
DEFAULT_GET_MAX_CHARS = 100_000
#: Search/parse work above this runs off the event loop.
CPU_IN_THREAD_CHARS = 256 * 1024
#: Token-estimate sample for very long texts.
ESTIMATE_SAMPLE_CHARS = 100_000
#: String leaves at least this big are called out in the passport.
LARGE_TEXT_CHARS = 200
MAX_LISTED_TEXTS = 10
PREVIEW_CHARS = 400

GET_NAME = "response_get"
FIND_NAME = "response_find"
WINDOW_NAME = "response_window"

NOT_FOUND_TEMPLATE = (
    "response '{ref}' is gone — it lived only for the task that fetched it "
    "(process restarts drop it too): run the call again for fresh data"
)
NO_KEY_TEMPLATE = "key '{key}' is not in this response; it has: {known}"
TEXT_HAS_NO_KEYS = "this response is plain text; call without a key"

MEGABYTE = 1024 * 1024


class ResponseNotFoundError(Exception):
    """The ref names no live response of this owner."""


@dataclass(frozen=True, slots=True)
class ResponseMemoryConfig:
    """Behavior knobs, mapped from the deployment's settings."""

    #: The wire ceiling of one remembered response (RAM, not context).
    max_response_chars: int = DEFAULT_MAX_RESPONSE_MB * MEGABYTE
    #: Process-wide LRU budget across every dialog this node runs.
    budget_chars: int = DEFAULT_BUDGET_MB * MEGABYTE
    #: What `response_get` returns when the caller does not choose.
    get_default_chars: int = DEFAULT_GET_CHARS
    #: The most a single deliberate read may take.
    get_max_chars: int = DEFAULT_GET_MAX_CHARS


@dataclass(slots=True)
class StoredResponse:
    """One parked document and what is known about it."""

    id: str
    owner_id: str
    #: The task that fetched it; dropped when that task's process terminates.
    scope: str
    #: "json" (document parsed) or "text" (anything else, held verbatim).
    kind: str
    source: str
    body: str
    document: Any = None
    last_access: float = 0.0

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX}{self.id}"


def dotted_get(value: Any, path: str) -> Any:  # noqa: ANN401 — parsed JSON in and out
    """Resolve a dotted path inside parsed JSON; None when any step misses.

    A twin of the collections module's helper on purpose: importing it from
    there would point a dependency arrow INTO the database tier from a module
    whose whole point is not needing one.
    """
    node = value
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def estimate_tokens(text: str) -> int:
    """An honest-enough token estimate from script composition.

    Sampled for huge inputs: the point is a decision aid ("3.4k tokens" vs
    "800k tokens"), not billing. Cyrillic ~2.4 chars/token, Latin ~4, the
    rest (digits, punctuation, markup) ~2.5.
    """
    sample = text[:ESTIMATE_SAMPLE_CHARS]
    cyrillic = len(re.findall(r"[а-яА-ЯёЁ]", sample))  # noqa: RUF001 — the whole point
    latin = len(re.findall(r"[a-zA-Z]", sample))
    other = len(sample) - cyrillic - latin
    estimate = cyrillic / 2.4 + latin / 4 + other / 2.5
    if len(text) > len(sample):
        estimate *= len(text) / len(sample)
    return int(estimate)


THOUSAND = 1000
KILOBYTE = 1024


def _human_tokens(count: int) -> str:
    if count >= THOUSAND:
        return f"~{count / THOUSAND:.1f}k tokens"
    return f"~{count} tokens"


class ResponseMemory:
    """The process-wide store; scoping and eviction, nothing else.

    Pure in-memory and synchronous on purpose: every operation is a dict
    move, and the runner's cleanup call must not await anything during
    process termination.
    """

    def __init__(self, config: ResponseMemoryConfig | None = None) -> None:
        self._config = config or ResponseMemoryConfig()
        self._items: dict[str, StoredResponse] = {}

    @property
    def config(self) -> ResponseMemoryConfig:
        return self._config

    def store(  # noqa: PLR0913, PLR0917 — one document's identity
        self,
        owner_id: str,
        scope: str,
        kind: str,
        source: str,
        body: str,
        document: Any = None,  # noqa: ANN401 — parsed JSON
    ) -> StoredResponse:
        """Park one document, evicting the least recently touched over budget."""
        item = StoredResponse(
            id=uuid.uuid4().hex[:12],
            owner_id=owner_id,
            scope=scope,
            kind=kind,
            source=source,
            body=body,
            document=document,
            last_access=utc_now().timestamp(),
        )
        self._items[item.id] = item
        self._evict()
        return item

    def get(self, owner_id: str, ref: str) -> StoredResponse:
        """The document, or `ResponseNotFoundError`; a read refreshes its LRU seat."""
        item = self._items.get(ref.removeprefix(REF_PREFIX))
        if item is None or item.owner_id != owner_id:
            raise ResponseNotFoundError(ref)
        item.last_access = utc_now().timestamp()
        return item

    def drop_scope(self, scope: str) -> None:
        """Forget every response of one task (the runner's termination hook)."""
        doomed = [key for key, item in self._items.items() if item.scope == scope]
        for key in doomed:
            del self._items[key]

    def _evict(self) -> None:
        while sum(len(item.body) for item in self._items.values()) > self._config.budget_chars:
            oldest = min(self._items.values(), key=lambda item: item.last_access)
            if len(self._items) == 1:
                return  # a single over-budget tenant stays; evicting it helps nobody
            del self._items[oldest.id]


# --- the passport -------------------------------------------------------------


def render_memory_passport(item: StoredResponse, config: ResponseMemoryConfig) -> str:
    """What the model sees instead of the body: numbers to decide with."""
    tokens = _human_tokens(estimate_tokens(item.body))
    head = (
        f"[response {item.ref}] kind={item.kind} · source={item.source or '-'} · "
        f"{_human_size(len(item.body))} · {tokens} · lives until this task ends\n"
    )
    if item.kind == "json":
        details = _describe_document(item.document)
    else:
        preview = item.body[:PREVIEW_CHARS].replace("\n", " ")
        details = f"preview: {preview}…\n"
    hint = (
        f"Read deliberately: response_get(key, max_chars up to {config.get_max_chars}) "
        "when the size fits your budget; response_find(pattern) + response_window(at) "
        "when it does not."
    )
    return head + details + hint


def _describe_document(document: Any) -> str:  # noqa: ANN401 — parsed JSON
    """Top-level keys with types, and every large text called out with sizes."""
    if not isinstance(document, dict):
        return ""
    parts: list[str] = []
    for name, value in document.items():
        parts.append(f"{name}: {_brief_type(value)}")
    texts = _large_texts(document)
    lines = f"keys: {{{', '.join(parts)}}}\n"
    if texts:
        listed = ", ".join(
            f"{path} ({len(text)} chars {_human_tokens(estimate_tokens(text))})"
            for path, text in texts[:MAX_LISTED_TEXTS]
        )
        lines += f"large text values: {listed}\n"
    return lines


def _brief_type(value: Any) -> str:  # noqa: ANN401 — parsed JSON
    if isinstance(value, str):
        return f"string({len(value)} chars)" if len(value) > LARGE_TEXT_CHARS else "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, dict):
        return f"object({len(value)} keys)"
    return "null"


def _large_texts(document: Any, prefix: str = "") -> list[tuple[str, str]]:  # noqa: ANN401
    found: list[tuple[str, str]] = []
    if isinstance(document, dict):
        for name, value in document.items():
            path = f"{prefix}.{name}" if prefix else str(name)
            if isinstance(value, str) and len(value) > LARGE_TEXT_CHARS:
                found.append((path, value))
            elif isinstance(value, dict):
                found.extend(_large_texts(value, path))
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found


def _human_size(chars: int) -> str:
    if chars >= MEGABYTE:
        return f"{chars / MEGABYTE:.1f} MB"
    if chars >= KILOBYTE:
        return f"{chars / KILOBYTE:.1f} KB"
    return f"{chars} chars"


# --- the three verbs ----------------------------------------------------------

GET_DESCRIPTION = (
    "Read a remembered response (ref 'resp:…' from a call's passport): the whole "
    "body, or one key of a JSON document (dotted path). The passport lists sizes — "
    "decide from them how much context to spend and pass max_chars accordingly; "
    "the default is conservative. For documents too big for any budget, search "
    "with response_find instead of reading."
)
FIND_DESCRIPTION = (
    "Search inside a remembered response ('resp:…'): a regex (or literal text) "
    "with a window of characters around every match. Answers the match count, and "
    "each match carries its 'at' position — widen a specific spot with "
    "response_window(at). Narrow the pattern rather than paging when there are "
    "too many matches."
)
WINDOW_DESCRIPTION = (
    "A window of a remembered response ('resp:…') around a position that "
    "response_find returned ('at'): more characters before/after the same spot."
)

GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "key": {
            "type": "string",
            "description": "Dotted key of a JSON document; omit for the whole body",
        },
        "max_chars": {
            "type": "integer",
            "description": "How much you are choosing to read (see the passport's sizes)",
        },
    },
    "required": ["ref"],
}
FIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "pattern": {"type": "string", "description": "Regex; an invalid one is taken literally"},
        "key": {
            "type": "string",
            "description": "Search inside this key's text instead of the whole body",
        },
        "before": {
            "type": "integer",
            "description": "Window chars before each match (default 300)",
        },
        "after": {"type": "integer", "description": "Window chars after each match (default 700)"},
        "max_matches": {"type": "integer", "description": "Matches per answer (default 5)"},
        "match_offset": {"type": "integer", "description": "Skip this many matches (paging)"},
    },
    "required": ["ref", "pattern"],
}
WINDOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "at": {"type": "integer", "description": "Position from a response_find match"},
        "key": {"type": "string", "description": "The key the find searched, if any"},
        "before": {"type": "integer", "description": "Chars before the position (default 1000)"},
        "after": {"type": "integer", "description": "Chars after the position (default 3000)"},
    },
    "required": ["ref", "at"],
}

FIND_DEFAULT_BEFORE = 300
FIND_DEFAULT_AFTER = 700
FIND_DEFAULT_MATCHES = 5
FIND_MAX_MATCHES = 20
WINDOW_DEFAULT_BEFORE = 1000
WINDOW_DEFAULT_AFTER = 3000


class _MemoryTool(Protocol):
    """The shared shape of the three verbs (spec/visibility plumbing)."""

    @property
    def spec(self) -> ToolSpec: ...


class _MemoryToolBase:
    def __init__(self, memory: ResponseMemory) -> None:
        self._memory = memory

    def visible_to(self, context: ToolContext) -> bool:
        """Responses only arrive through HTTP calls; same plan gate."""
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    def _target(self, item: StoredResponse, key: str | None) -> str:
        """The text a verb operates on: the whole body, or one key's value."""
        if key is None:
            return item.body
        if item.kind != "json":
            raise ToolArgumentsError(TEXT_HAS_NO_KEYS)
        value = dotted_get(item.document, key)
        if value is None:
            known = (
                ", ".join(sorted(item.document)) if isinstance(item.document, dict) else "(no keys)"
            )
            raise ToolArgumentsError(NO_KEY_TEMPLATE.format(key=key, known=known))
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)


class ResponseGetTool(_MemoryToolBase):
    """The deliberate read: the model chose to spend this much context."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=GET_NAME, description=GET_DESCRIPTION, parameters_schema=GET_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        key = _parse_optional_str(arguments.get("key"), "key")
        config = self._memory.config
        asked = _parse_positive(arguments.get("max_chars"), config.get_default_chars, "max_chars")
        cap = min(asked, config.get_max_chars)
        try:
            item = self._memory.get(context.user_id, ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, key)
        if len(target) <= cap:
            return target
        return (
            f"{target[:cap]}\n…[showing {cap} of {len(target)} chars "
            f"({_human_tokens(estimate_tokens(target))} total); raise max_chars "
            f"(ceiling {config.get_max_chars}) or search with response_find]"
        )


class ResponseFindTool(_MemoryToolBase):
    """Grep with windows: for documents no budget should swallow whole."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=FIND_NAME, description=FIND_DESCRIPTION, parameters_schema=FIND_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolArgumentsError("pattern must be a non-empty string")
        key = _parse_optional_str(arguments.get("key"), "key")
        before = _parse_positive(arguments.get("before"), FIND_DEFAULT_BEFORE, "before")
        after = _parse_positive(arguments.get("after"), FIND_DEFAULT_AFTER, "after")
        max_matches = min(
            _parse_positive(arguments.get("max_matches"), FIND_DEFAULT_MATCHES, "max_matches"),
            FIND_MAX_MATCHES,
        )
        offset = _parse_positive(arguments.get("match_offset"), 0, "match_offset", floor=0)
        try:
            item = self._memory.get(context.user_id, ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, key)
        if len(target) > CPU_IN_THREAD_CHARS:
            positions = await asyncio.to_thread(_find_positions, target, pattern)
        else:
            positions = _find_positions(target, pattern)
        if not positions:
            return f"no matches for {pattern!r} in {len(target)} chars"
        shown = positions[offset : offset + max_matches]
        windows = _merge_windows(
            [(max(0, at - before), min(len(target), end + after), at) for at, end in shown]
        )
        blocks = [f"[at={at}] …{target[start:end]}…" for start, end, at in windows]
        more = " — narrow the pattern or page with match_offset"
        summary = f"{len(positions)} match(es); showing {offset + 1}-{offset + len(shown)}" + (
            more if len(positions) > len(shown) else ""
        )
        return summary + "\n" + "\n---\n".join(blocks)


class ResponseWindowTool(_MemoryToolBase):
    """A bigger look at a spot a find already located."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=WINDOW_NAME, description=WINDOW_DESCRIPTION, parameters_schema=WINDOW_SCHEMA
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        at = arguments.get("at")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ToolArgumentsError("at must be a position from a response_find match")
        key = _parse_optional_str(arguments.get("key"), "key")
        before = _parse_positive(arguments.get("before"), WINDOW_DEFAULT_BEFORE, "before")
        after = _parse_positive(arguments.get("after"), WINDOW_DEFAULT_AFTER, "after")
        try:
            item = self._memory.get(context.user_id, ref)
        except ResponseNotFoundError:
            return NOT_FOUND_TEMPLATE.format(ref=arguments.get("ref"))
        target = self._target(item, key)
        if at >= len(target):
            return f"position {at} is past the end ({len(target)} chars)"
        start, end = max(0, at - before), min(len(target), at + after)
        return f"[{start}-{end} of {len(target)}] …{target[start:end]}…"


def _find_positions(target: str, pattern: str) -> list[tuple[int, int]]:
    """(start, end) of every match; an invalid regex is searched literally."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        compiled = re.compile(re.escape(pattern), re.IGNORECASE)
    return [(match.start(), match.end()) for match in compiled.finditer(target)]


def _merge_windows(windows: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Overlapping windows collapse into one (dense matches must not duplicate text)."""
    merged: list[tuple[int, int, int]] = []
    for start, end, at in sorted(windows):
        if merged and start <= merged[-1][1]:
            last_start, last_end, last_at = merged[-1]
            merged[-1] = (last_start, max(last_end, end), last_at)
        else:
            merged.append((start, end, at))
    return merged


def _parse_ref(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("ref must be a non-empty string like 'resp:…'")
    return raw.strip()


def _parse_optional_str(raw: object, name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{name} must be a non-empty string")
    return raw.strip()


def _parse_positive(raw: object, default: int, name: str, floor: int = 1) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < floor:
        raise ToolArgumentsError(f"{name} must be an integer >= {floor}")
    return raw
