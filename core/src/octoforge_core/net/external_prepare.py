"""Prepare a classic endpoint call in security-sensitive substitution order."""

from octoforge_core.net.external_headers import (
    HeaderRenderRequest,
    auth_headers_for,
    render_headers,
)
from octoforge_core.net.external_rendering import (
    UrlRenderRequest,
    collect_plan,
    render_safe_url,
    secret_sentinels,
    substitute_url_secrets,
)
from octoforge_core.net.external_types import CallCredentials, PreparedHttpCall
from octoforge_core.net.external_values import ExternalValues, enforce_placements
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.spec_templates import render_template
from octoforge_core.net.spec_types import ToolSpec


class ExternalCallPreparer:
    """Validate, guard, resolve credentials and render one HTTP request."""

    def __init__(self, guard: SsrfGuard, credentials: CallCredentials) -> None:
        self._guard = guard
        self._credentials = credentials
        self._values = ExternalValues(credentials)

    async def prepare(
        self,
        spec: ToolSpec,
        validated: dict[str, str],
        user_id: str | None,
    ) -> PreparedHttpCall:
        plan = collect_plan(spec)
        user_values = await self._values.user_values(plan.combined, user_id)
        sentinels = secret_sentinels(plan)
        safe_url = render_safe_url(UrlRenderRequest(spec, validated, user_values, sentinels))
        await self._guard.check(safe_url)
        secrets = await self._values.secrets(plan.combined, safe_url, user_id)
        enforce_placements(plan.placements, secrets)
        url = substitute_url_secrets(safe_url, sentinels, secrets)
        render_values = {
            **validated,
            **user_values,
            **{f"secret.{code}": secret.value for code, secret in secrets.items()},
        }
        headers = render_headers(
            HeaderRenderRequest(
                spec,
                plan,
                render_values,
                auth_headers_for(url, user_id, self._credentials.auth_whitelist),
            )
        )
        body = (
            render_template(spec.body_template, render_values)
            if spec.body_template is not None
            else None
        )
        return PreparedHttpCall(spec.method, url, headers, body, tuple(secrets.values()))
