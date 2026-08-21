"""Cross-field validation of an endpoint contract."""

from dataclasses import dataclass

from octoforge_core.net.errors import ToolSpecError
from octoforge_core.net.spec_templates import collect_refs
from octoforge_core.net.spec_types import ParamKind, ToolParamSpec

SPEC_KEYS = frozenset(
    {
        "method",
        "url_template",
        "params_schema",
        "body_template",
        "headers",
        "auth",
        "response",
        "pagination",
    }
)
ANNOTATION_KEYS = frozenset({"notes", "description"})
KNOWN_KEYS = SPEC_KEYS | ANNOTATION_KEYS


def reject_unknown_keys(data: dict[str, object]) -> None:
    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise ToolSpecError(
            f"unknown field(s) in the endpoint document: {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(KNOWN_KEYS))}"
        )


@dataclass(frozen=True, slots=True)
class ContractTemplates:
    url: str
    body: str | None
    headers: dict[str, str]
    params: dict[str, ToolParamSpec]


def validate_templates(
    contract: ContractTemplates,
) -> None:
    _validate_template_fields("url_template", contract.url, contract.params)
    _validate_url_authority(contract.url, contract.params)
    if contract.body is not None:
        _validate_template_fields("body_template", contract.body, contract.params)
    for name, value in contract.headers.items():
        _validate_template_fields(f"headers[{name!r}]", value, contract.params)


def _validate_template_fields(
    label: str,
    template: str,
    params: dict[str, ToolParamSpec],
) -> None:
    try:
        refs = collect_refs(template)
    except ToolSpecError as exc:
        raise ToolSpecError(f"{label}: {exc}") from None
    for field_name in sorted(refs.params):
        param = params.get(field_name)
        if param is None:
            raise ToolSpecError(f"{label} references undeclared parameter: {field_name!r}")
        if not param.required:
            raise ToolSpecError(f"{label} parameter must be required: {field_name!r}")


def _validate_url_authority(
    url_template: str,
    params: dict[str, ToolParamSpec],
) -> None:
    scheme_end = url_template.find("://")
    path_start = url_template.find("/", scheme_end + 3 if scheme_end >= 0 else 0)
    prefix = url_template if path_start < 0 else url_template[:path_start]
    refs = collect_refs(prefix)
    if refs.secrets:
        raise ToolSpecError("a secret placeholder cannot appear in the URL scheme or host")
    for field_name in sorted(refs.params):
        param = params.get(field_name)
        if param is not None and param.kind is not ParamKind.HOST:
            raise ToolSpecError(
                f"url_template puts {field_name!r} in the host, but it is a "
                f"{param.kind.value!r} param: only a 'host' param (with its own "
                "'hosts' allowlist) may decide where the request goes"
            )
