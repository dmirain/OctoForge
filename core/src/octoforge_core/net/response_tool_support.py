"""Shared visibility, target selection, and argument parsing for response tools."""

import json
from dataclasses import dataclass
from typing import Protocol, cast

from octoforge_core.net.response_models import (
    NO_KEY_TEMPLATE,
    TEXT_HAS_NO_KEYS,
    DocumentHome,
    ResponseMemoryConfig,
    StoredDocument,
)
from octoforge_core.net.response_passport import dotted_get
from octoforge_core.tariffs.api import FeatureCode, feature_enabled
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError


class MemoryTool(Protocol):
    """Shared interface of the three response-reading verbs."""

    @property
    def spec(self) -> ToolSpec: ...


class MemoryToolBase:
    """Home/config plumbing and JSON target selection."""

    def __init__(self, home: DocumentHome, config: ResponseMemoryConfig | None = None) -> None:
        self._home = home
        self._config = config or ResponseMemoryConfig()

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    def _target(self, item: StoredDocument, key: str | None) -> str:
        if key is None:
            return item.body
        if item.kind != "json":
            raise ToolArgumentsError(TEXT_HAS_NO_KEYS)
        value = dotted_get(item.document, key)
        if value is None:
            known = _known_keys(item.document)
            raise ToolArgumentsError(NO_KEY_TEMPLATE.format(key=key, known=known))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _known_keys(document: object) -> str:
    if not isinstance(document, dict):
        return "(no keys)"
    return ", ".join(sorted(cast("dict[str, object]", document)))


@dataclass(frozen=True, slots=True)
class PositiveRule:
    """Validation rule for an optional integer argument."""

    name: str
    default: int
    floor: int = 1


def parse_ref(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("ref must be a non-empty string like 'resp:…'")
    return raw.strip()


def parse_optional_str(raw: object, name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{name} must be a non-empty string")
    return raw.strip()


def parse_positive(raw: object, rule: PositiveRule) -> int:
    if raw is None:
        return rule.default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < rule.floor:
        raise ToolArgumentsError(f"{rule.name} must be an integer >= {rule.floor}")
    return raw
