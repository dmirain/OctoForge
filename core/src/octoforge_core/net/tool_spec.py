"""Parse endpoint instruction records into validated executable specs."""

from octoforge_core.net.spec_auth import parse_auth
from octoforge_core.net.spec_fields import (
    load_json,
    parse_headers,
    parse_optional_text,
    parse_params_schema,
    parse_required_text,
)
from octoforge_core.net.spec_method import parse_method
from octoforge_core.net.spec_response import parse_pagination, parse_response
from octoforge_core.net.spec_templates import collect_refs, render_template
from octoforge_core.net.spec_types import (
    DEFAULT_AUTH,
    FieldCoercion,
    PaginationKind,
    PaginationSpec,
    ParamKind,
    ResponseSpec,
    TemplateRefs,
    ToolParamSpec,
    ToolSpec,
)
from octoforge_core.net.spec_validation import (
    ContractTemplates,
    reject_unknown_keys,
    validate_templates,
)

__all__ = [
    "FieldCoercion",
    "PaginationKind",
    "PaginationSpec",
    "ParamKind",
    "ResponseSpec",
    "TemplateRefs",
    "ToolParamSpec",
    "ToolSpec",
    "collect_refs",
    "parse_tool_spec",
    "render_template",
]


def parse_tool_spec(content: str) -> ToolSpec:
    """Parse and cross-check one JSON endpoint contract."""
    data = load_json(content)
    reject_unknown_keys(data)
    method = parse_method(data.get("method"))
    url_template = parse_required_text(data.get("url_template"), "url_template")
    params = parse_params_schema(data.get("params_schema"))
    body_template = parse_optional_text(data.get("body_template"), "body_template")
    headers = parse_headers(data.get("headers"))
    auth, expanded = parse_auth(data.get("auth", DEFAULT_AUTH))
    if expanded is not None:
        header_name, header_template = expanded
        headers[header_name] = header_template
    validate_templates(ContractTemplates(url_template, body_template, headers, params))
    return ToolSpec(
        method,
        url_template,
        params,
        auth,
        body_template,
        headers,
        parse_response(data.get("response")),
        parse_pagination(data.get("pagination"), params),
    )
