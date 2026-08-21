"""Validate a routing tool call into the actor's safe decision model."""

import logging

from octoforge_core.agent.router_types import RouteAction, RouteDecision
from octoforge_core.dialogs.api import TITLE_MAX_LENGTH

logger = logging.getLogger(__name__)


def parse_decision(arguments: dict[str, object], known_ids: set[str]) -> RouteDecision:
    cancel_ids = tuple(
        value
        for value in _as_list(arguments.get("cancel_exchange_ids"))
        if isinstance(value, str) and value in known_ids
    )
    try:
        action = RouteAction(str(arguments.get("action")))
    except ValueError:
        logger.warning("router action unusable, defaulting to a new exchange: %r", arguments)
        return RouteDecision(cancel_ids=cancel_ids)
    target = arguments.get("exchange_id")
    if action is not RouteAction.CONTINUE:
        return RouteDecision(action=action, cancel_ids=cancel_ids)
    if not isinstance(target, str) or target not in known_ids:
        logger.warning("router continue without a known exchange: %r", arguments)
        return RouteDecision(cancel_ids=cancel_ids)
    return RouteDecision(
        action=action,
        exchange_id=target,
        cancel_ids=cancel_ids,
        title=_clean_title(arguments.get("title")),
    )


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    return title[:TITLE_MAX_LENGTH] if title else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
