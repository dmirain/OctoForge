"""Rendering and JSON navigation for remembered-response passports."""

import re
from typing import cast

from octoforge_core.net.response_models import MEGABYTE, ResponseMemoryConfig, StoredDocument

ESTIMATE_SAMPLE_CHARS = 100_000
LARGE_TEXT_CHARS = 200
MAX_LISTED_TEXTS = 10
PREVIEW_CHARS = 400
THOUSAND = 1000
KILOBYTE = 1024


def dotted_get(value: object, path: str) -> object:
    """Resolve a dotted path inside parsed JSON; None when any step misses."""
    node = value
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = cast("dict[str, object]", node).get(part)
    return node


def estimate_tokens(text: str) -> int:
    """Estimate token cost from script composition, sampling huge inputs."""
    sample = text[:ESTIMATE_SAMPLE_CHARS]
    cyrillic = len(re.findall(r"[\u0430-\u044f\u0410-\u042f\u0451\u0401]", sample))
    latin = len(re.findall(r"[a-zA-Z]", sample))
    other = len(sample) - cyrillic - latin
    estimate = cyrillic / 2.4 + latin / 4 + other / 2.5
    if len(text) > len(sample):
        estimate *= len(text) / len(sample)
    return int(estimate)


def render_document_passport(
    doc: StoredDocument, config: ResponseMemoryConfig, lifetime: str
) -> str:
    """Render the size, shape, lifetime, and reading instructions."""
    tokens = _human_tokens(estimate_tokens(doc.body))
    head = (
        f"[response {doc.ref}] kind={doc.kind} · source={doc.source or '-'} · "
        f"{_human_size(len(doc.body))} · {tokens} · {lifetime}\n"
    )
    details = _describe_document(doc.document) if doc.kind == "json" else _preview(doc.body)
    hint = (
        f"Read deliberately: response_get(key, max_chars up to {config.get_max_chars}) "
        "when the size fits your budget; response_find(pattern) + response_window(at) "
        "when it does not."
    )
    return head + details + hint


def _preview(body: str) -> str:
    return f"preview: {body[:PREVIEW_CHARS].replace(chr(10), ' ')}…\n"


def _describe_document(document: object) -> str:
    if not isinstance(document, dict):
        return ""
    values = cast("dict[str, object]", document)
    parts = [f"{name}: {_brief_type(value)}" for name, value in values.items()]
    texts = _large_texts(values)
    lines = f"keys: {{{', '.join(parts)}}}\n"
    if texts:
        listed = ", ".join(_describe_text(path, text) for path, text in texts[:MAX_LISTED_TEXTS])
        lines += f"large text values: {listed}\n"
    return lines


def _describe_text(path: str, text: str) -> str:
    return f"{path} ({len(text)} chars {_human_tokens(estimate_tokens(text))})"


def _brief_type(value: object) -> str:
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


def _large_texts(document: dict[str, object], prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name, value in document.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, str) and len(value) > LARGE_TEXT_CHARS:
            found.append((path, value))
        elif isinstance(value, dict):
            found.extend(_large_texts(cast("dict[str, object]", value), path))
    return sorted(found, key=lambda pair: len(pair[1]), reverse=True)


def _human_tokens(count: int) -> str:
    return f"~{count / THOUSAND:.1f}k tokens" if count >= THOUSAND else f"~{count} tokens"


def _human_size(chars: int) -> str:
    if chars >= MEGABYTE:
        return f"{chars / MEGABYTE:.1f} MB"
    if chars >= KILOBYTE:
        return f"{chars / KILOBYTE:.1f} KB"
    return f"{chars} chars"
