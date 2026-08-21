"""Describe and log the object graph assembled for this installation."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.composition import LexicalBackend

from octoforge_server.capability_llm import llm_capabilities, multimodal_capabilities
from octoforge_server.capability_model import CRITICAL, REPORT_HEADER, Capability
from octoforge_server.capability_search import search_capabilities
from octoforge_server.capability_tools import (
    retention_capabilities,
    surface_capabilities,
    tool_capabilities,
)
from octoforge_server.config import Settings


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    search_extensions: frozenset[str] = frozenset()
    lexical_backend: LexicalBackend = LexicalBackend.NONE
    extra: Sequence[Capability] = ()


DEFAULT_CAPABILITY_REPORT = CapabilityReport()


def describe_capabilities(
    settings: Settings,
    search_extensions: frozenset[str] = frozenset(),
    lexical_backend: LexicalBackend = LexicalBackend.NONE,
) -> tuple[Capability, ...]:
    return (
        *llm_capabilities(settings),
        *multimodal_capabilities(settings),
        *tool_capabilities(settings),
        *surface_capabilities(settings),
        *retention_capabilities(settings),
        *search_capabilities(search_extensions, lexical_backend),
    )


def log_capabilities(
    settings: Settings,
    logger: logging.Logger,
    report: CapabilityReport = DEFAULT_CAPABILITY_REPORT,
) -> None:
    capabilities = (
        *describe_capabilities(
            settings,
            report.search_extensions,
            report.lexical_backend,
        ),
        *report.extra,
    )
    logger.info("%s\n%s", REPORT_HEADER, "\n".join(cap.line() for cap in capabilities))
    for capability in capabilities:
        if not capability.enabled and capability.name in CRITICAL:
            logger.warning("%s is off: %s", capability.name, capability.detail)


__all__ = [
    "CRITICAL",
    "Capability",
    "CapabilityReport",
    "describe_capabilities",
    "log_capabilities",
]
