"""Validation and common feature-gating responses for tariffs."""

import re

from octoforge_core.tariffs.types import InvalidTariffError

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_TITLE_CHARS = 256
FEATURE_REFUSAL_TEMPLATE = "not available on the current plan: {feature}"


def feature_enabled(features: frozenset[str] | None, feature: str) -> bool:
    return features is None or feature in features


def feature_refusal(feature: str) -> str:
    return FEATURE_REFUSAL_TEMPLATE.format(feature=feature)


def normalize_feature(raw: str) -> str:
    feature = raw.strip().lower()
    if not CODE_PATTERN.match(feature):
        raise InvalidTariffError(
            "feature code must be 1-64 characters of [a-z0-9_], e.g. 'web_search'"
        )
    return feature


def normalize_code(raw: str) -> str:
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidTariffError("tariff code must be 1-64 characters of [a-z0-9_], e.g. 'basic'")
    return code


def normalize_title(raw: str) -> str:
    title = raw.strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise InvalidTariffError(f"tariff title must be 1..{MAX_TITLE_CHARS} characters")
    return title
