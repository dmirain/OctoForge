"""Stable public boundary of stored endpoint execution."""

from octoforge_core.net.external_executor import ExternalCallExecutor
from octoforge_core.net.external_http import read_capped_text
from octoforge_core.net.external_messages import (
    MAX_BODY_CHARS,
    SECRET_MISSING_TEMPLATE,
    SECRETS_DISABLED_MESSAGE,
    TRUNCATED_SUFFIX,
)
from octoforge_core.net.external_types import (
    CallCredentials,
    CallOptions,
    ExternalCallAuth,
    ExternalCallConfig,
    ExternalCallContext,
    ExternalCallResult,
    ExternalCallServices,
    KindCallDelegate,
    KindCallRequest,
)
from octoforge_core.net.external_values import scrub

_scrub = scrub

__all__ = [
    "MAX_BODY_CHARS",
    "SECRETS_DISABLED_MESSAGE",
    "SECRET_MISSING_TEMPLATE",
    "TRUNCATED_SUFFIX",
    "CallCredentials",
    "CallOptions",
    "ExternalCallAuth",
    "ExternalCallConfig",
    "ExternalCallContext",
    "ExternalCallExecutor",
    "ExternalCallResult",
    "ExternalCallServices",
    "KindCallDelegate",
    "KindCallRequest",
    "read_capped_text",
]
