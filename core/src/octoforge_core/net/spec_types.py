"""Typed executable view of an endpoint contract."""

from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_AUTH = "none"


class ParamKind(StrEnum):
    STRING = "string"
    PATH = "path"
    HOST = "host"


class FieldCoercion(StrEnum):
    """Projection coercion applied before response records are stored."""

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class PaginationKind(StrEnum):
    """How an endpoint advances through pages."""

    PAGE = "page"
    OFFSET = "offset"
    CURSOR = "cursor"


@dataclass(frozen=True, slots=True)
class ToolParamSpec:
    required: bool
    kind: ParamKind = ParamKind.STRING
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRefs:
    params: frozenset[str] = frozenset()
    user_params: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()

    def __or__(self, other: "TemplateRefs") -> "TemplateRefs":
        return TemplateRefs(
            self.params | other.params,
            self.user_params | other.user_params,
            self.secrets | other.secrets,
        )


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """How a response becomes collection records."""

    items_path: str | None = None
    fields: dict[str, FieldCoercion] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaginationSpec:
    """Inputs needed to walk a paginated endpoint."""

    kind: PaginationKind
    param: str
    start: int = 0
    cursor_path: str | None = None
    total_path: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    method: str
    url_template: str
    params: dict[str, ToolParamSpec] = field(default_factory=dict)
    auth: str = DEFAULT_AUTH
    body_template: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    response: ResponseSpec | None = None
    pagination: PaginationSpec | None = None
