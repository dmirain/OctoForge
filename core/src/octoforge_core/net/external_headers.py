"""Render endpoint headers and merge installation credentials safely."""

from collections.abc import Mapping
from dataclasses import dataclass

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external_messages import USER_ID_PLACEHOLDER
from octoforge_core.net.external_rendering import RenderPlan
from octoforge_core.net.external_types import ExternalCallAuth
from octoforge_core.net.guard import matches_url_prefix
from octoforge_core.net.spec_templates import render_template
from octoforge_core.net.spec_types import ToolSpec


@dataclass(frozen=True, slots=True)
class HeaderRenderRequest:
    spec: ToolSpec
    plan: RenderPlan
    values: Mapping[str, str]
    whitelist_headers: Mapping[str, str]


def render_headers(
    request: HeaderRenderRequest,
) -> dict[str, str]:
    plain: dict[str, str] = {}
    secret_bearing: dict[str, str] = {}
    for name, template in request.spec.headers.items():
        rendered = render_template(template, request.values)
        if not all(" " <= char <= "~" for char in rendered):
            raise ExternalCallError(f"header {name!r} renders to an illegal value")
        target = secret_bearing if request.plan.header_refs[name].secrets else plain
        target[name] = rendered
    return {**plain, **request.whitelist_headers, **secret_bearing}


def auth_headers_for(
    url: str,
    user_id: str | None,
    whitelist: tuple[ExternalCallAuth, ...],
) -> dict[str, str]:
    for entry in whitelist:
        if matches_url_prefix(url, entry.base_url_prefix):
            if USER_ID_PLACEHOLDER not in entry.header_value:
                return {entry.header_name: entry.header_value}
            if user_id is not None:
                return {entry.header_name: entry.header_value.replace(USER_ID_PLACEHOLDER, user_id)}
    return {}
