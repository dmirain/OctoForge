"""Endpoint placeholder parsing and verbatim rendering."""

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field

from octoforge_core.net.errors import ToolSpecError
from octoforge_core.net.spec_types import TemplateRefs

USER_NAMESPACE = "user"
SECRET_NAMESPACE = "secret"
REF_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


def collect_refs(template: str) -> TemplateRefs:
    """Collect validated placeholder names by namespace."""
    collector = _RefCollector()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ToolSpecError(f"malformed template: {exc}") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        _validate_field(field_name, format_spec or "", conversion)
        collector.add(field_name)
    return collector.refs()


def render_template(template: str, values: Mapping[str, str]) -> str:
    parts: list[str] = []
    for literal, field_name, _, _ in string.Formatter().parse(template):
        parts.append(literal)
        if field_name is not None:
            parts.append(values[field_name])
    return "".join(parts)


def _validate_field(field_name: str, format_spec: str, conversion: str | None) -> None:
    if not field_name:
        raise ToolSpecError("empty placeholder {} is not allowed in templates")
    if format_spec or conversion:
        raise ToolSpecError(
            f"placeholder {{{field_name}}} carries a format spec or conversion; "
            "substitution is verbatim - remove it"
        )


def _validate_ref_code(field_name: str, code: str) -> None:
    if not REF_CODE_PATTERN.match(code):
        raise ToolSpecError(
            f"placeholder {{{field_name}}} must reference a code of 1-64 characters of [a-z0-9_]"
        )


@dataclass(slots=True)
class _RefCollector:
    params: set[str] = field(default_factory=set)
    user_params: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)

    def add(self, field_name: str) -> None:
        namespace, separator, code = field_name.partition(".")
        if not separator:
            self.params.add(field_name)
            return
        if namespace == USER_NAMESPACE:
            _validate_ref_code(field_name, code)
            self.user_params.add(code)
            return
        if namespace == SECRET_NAMESPACE:
            _validate_ref_code(field_name, code)
            self.secrets.add(code)
            return
        raise ToolSpecError(
            f"unknown namespace in placeholder {{{field_name}}}: "
            f"only '{USER_NAMESPACE}.' and '{SECRET_NAMESPACE}.' exist"
        )

    def refs(self) -> TemplateRefs:
        return TemplateRefs(
            frozenset(self.params),
            frozenset(self.user_params),
            frozenset(self.secrets),
        )
