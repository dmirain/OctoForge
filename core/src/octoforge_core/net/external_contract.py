"""Stored endpoint kind detection and pagination seeding."""

import json
from typing import Any

from octoforge_core.net.external_types import ExternalCallContext
from octoforge_core.net.spec_types import ToolSpec


def pagination_seed(
    spec: ToolSpec,
    params: dict[str, Any],
    context: ExternalCallContext,
) -> dict[str, Any]:
    if not context.options.collect or spec.pagination is None:
        return params
    return {**params, spec.pagination.param: params.get(spec.pagination.param, "")}


def content_kind(content: str) -> str | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    return kind if isinstance(kind, str) and kind else None
