"""Render validated endpoint specs into guarded HTTP request material."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.spec_templates import collect_refs, render_template
from octoforge_core.net.spec_types import ParamKind, TemplateRefs, ToolParamSpec, ToolSpec
from octoforge_core.secrets.api import ResolvedSecret, SecretPlacement


@dataclass(frozen=True, slots=True)
class RenderPlan:
    url_refs: TemplateRefs
    body_refs: TemplateRefs
    header_refs: dict[str, TemplateRefs]

    @property
    def combined(self) -> TemplateRefs:
        refs = self.url_refs | self.body_refs
        for header in self.header_refs.values():
            refs = refs | header
        return refs

    @property
    def placements(self) -> tuple[tuple[SecretPlacement, frozenset[str]], ...]:
        header_secrets = (
            frozenset().union(*(refs.secrets for refs in self.header_refs.values()))
            if self.header_refs
            else frozenset()
        )
        return (
            (SecretPlacement.URL, self.url_refs.secrets),
            (SecretPlacement.BODY, self.body_refs.secrets),
            (SecretPlacement.HEADER, header_secrets),
        )


@dataclass(frozen=True, slots=True)
class UrlRenderRequest:
    spec: ToolSpec
    params: dict[str, str]
    user_values: Mapping[str, str]
    sentinels: Mapping[str, str]


def collect_plan(spec: ToolSpec) -> RenderPlan:
    return RenderPlan(
        collect_refs(spec.url_template),
        collect_refs(spec.body_template) if spec.body_template is not None else TemplateRefs(),
        {name: collect_refs(value) for name, value in spec.headers.items()},
    )


def secret_sentinels(plan: RenderPlan) -> dict[str, str]:
    return {code: f"of-secret-{uuid.uuid4().hex}" for code in plan.url_refs.secrets}


def render_safe_url(
    request: UrlRenderRequest,
) -> str:
    values = {
        name: _escape_for_url(request.spec.params[name], value)
        for name, value in request.params.items()
    }
    values.update({name: quote(value, safe="") for name, value in request.user_values.items()})
    values.update({f"secret.{code}": sentinel for code, sentinel in request.sentinels.items()})
    return render_template(request.spec.url_template, values)


def substitute_url_secrets(
    safe_url: str,
    sentinels: Mapping[str, str],
    secrets: Mapping[str, ResolvedSecret],
) -> str:
    netloc = urlsplit(safe_url).netloc
    url = safe_url
    for code, sentinel in sentinels.items():
        if sentinel in netloc:
            raise ExternalCallError("a secret placeholder cannot appear in the URL host")
        url = url.replace(sentinel, quote(secrets[code].value, safe=""))
    return url


def _escape_for_url(spec: ToolParamSpec, value: str) -> str:
    match spec.kind:
        case ParamKind.STRING:
            return quote(value, safe="")
        case ParamKind.PATH:
            return "/".join(quote(segment, safe="") for segment in value.split("/"))
        case ParamKind.HOST:
            return value
