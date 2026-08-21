"""Public boundary of the tariffs module."""

from octoforge_core.tariffs.policy import (
    feature_enabled,
    feature_refusal,
    normalize_code,
    normalize_feature,
    normalize_title,
)
from octoforge_core.tariffs.ports import LimitGate, TariffStore, UsageMeter, UsageRecorder
from octoforge_core.tariffs.types import (
    CORE_FEATURES,
    FeatureCode,
    InvalidTariffError,
    Tariff,
    TariffDefinition,
    TariffInUseError,
    TariffLimits,
    TariffNotFoundError,
    normalize_limit,
)
from octoforge_core.tariffs.usage_types import (
    LimitVerdict,
    UsageEvent,
    UsageKind,
    UsageOrigin,
    UsageTotals,
)

__all__ = [
    "CORE_FEATURES",
    "FeatureCode",
    "InvalidTariffError",
    "LimitGate",
    "LimitVerdict",
    "Tariff",
    "TariffDefinition",
    "TariffInUseError",
    "TariffLimits",
    "TariffNotFoundError",
    "TariffStore",
    "UsageEvent",
    "UsageKind",
    "UsageMeter",
    "UsageOrigin",
    "UsageRecorder",
    "UsageTotals",
    "feature_enabled",
    "feature_refusal",
    "normalize_code",
    "normalize_feature",
    "normalize_limit",
    "normalize_title",
]
