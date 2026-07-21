"""Tests for the tool spec parser and the external call executor."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
import pytest

from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
)
from octoforge_core.net.errors import ExternalCallError, SsrfBlockedError, ToolSpecError
from octoforge_core.net.external import (
    MAX_BODY_CHARS,
    TRUNCATED_SUFFIX,
    ExternalCallAuth,
    ExternalCallExecutor,
)
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tool_spec import parse_tool_spec
from octoforge_core.net.tools import ExternalCallSkill
from octoforge_core.skills.base import SkillContext

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.1"
TOOL_NAME = "wttr_in_weather"
INTERNAL_TOOL_NAME = "internal_api"
INTERNAL_PREFIX = "https://api.internal.example.com/"
AUTH_HEADER = "X-Api-Key"
AUTH_VALUE = "s3cret"
REDIRECT_TARGET = "http://169.254.169.254/latest"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

WEATHER_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": "https://wttr.in/{city}?format=j2",
        "params_schema": {"city": {"type": "string", "required": True}},
        "auth": "none",
    }
)
INTERNAL_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": "https://api.internal.example.com/v1/status/{service}",
        "params_schema": {
            "service": {"type": "string", "required": True},
            "verbose": {"type": "string", "required": False},
        },
        "auth": "none",
    }
)


class StubResolver:
    """HostResolver returning a scripted set of addresses."""

    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips

    async def resolve(self, host: str) -> tuple[str, ...]:
        return self._ips


class FakeInstructionService:
    """InstructionService stub serving scripted tool records by title."""

    def __init__(self, records: dict[str, str]) -> None:
        self._records = records

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        if name not in self._records:
            raise InstructionNotFoundError(name)
        return Instruction(
            id=f"id-{name}",
            type=InstructionType.ENDPOINT,
            title=name,
            content=self._records[name],
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        )

    async def search(self, query: str, k: int) -> list[SearchHit]:
        raise NotImplementedError

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        raise NotImplementedError


def make_executor(
    handler: Callable[[httpx.Request], httpx.Response],
    records: dict[str, str] | None = None,
    ips: tuple[str, ...] = (PUBLIC_IP,),
    whitelist: tuple[ExternalCallAuth, ...] = (),
    allowed_prefixes: tuple[str, ...] = (),
) -> ExternalCallExecutor:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExternalCallExecutor(
        service=FakeInstructionService(records or {TOOL_NAME: WEATHER_TOOL_CONTENT}),
        http_client=http_client,
        guard=SsrfGuard(resolver=StubResolver(ips), allowed_prefixes=allowed_prefixes),
        auth_whitelist=whitelist,
    )


def ok_handler(body: str = "{}") -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(HTTPStatus.OK, text=body)


# --- parse_tool_spec -------------------------------------------------------


def test_parse_minimal_spec_defaults_params_and_auth() -> None:
    spec = parse_tool_spec(json.dumps({"method": "get", "url_template": "https://x.test/"}))

    assert spec.method == "GET"
    assert spec.url_template == "https://x.test/"
    assert spec.params == {}
    assert spec.auth == "none"


def test_parse_full_spec() -> None:
    spec = parse_tool_spec(WEATHER_TOOL_CONTENT)

    assert spec.method == "GET"
    assert spec.params["city"].required is True


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(["GET", "https://x.test/"]),  # not an object
        json.dumps({"url_template": "https://x.test/"}),  # missing method
        json.dumps({"method": "TELEPORT", "url_template": "https://x.test/"}),
        json.dumps({"method": "GET"}),  # missing url_template
        json.dumps({"method": "GET", "url_template": ""}),
        json.dumps({"method": "GET", "url_template": "https://x.test/", "params_schema": []}),
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://x.test/{n}",
                "params_schema": {"n": {"type": "integer"}},
            }
        ),
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://x.test/{city}",
                "params_schema": {"city": {"type": "string", "required": "yes"}},
            }
        ),
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://x.test/{unknown}",
                "params_schema": {"city": {"type": "string"}},
            }
        ),
    ],
)
def test_parse_invalid_specs_raise(content: str) -> None:
    with pytest.raises(ToolSpecError):
        parse_tool_spec(content)


# --- execute ---------------------------------------------------------------


async def test_execute_renders_template_and_returns_status_and_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text='{"temp_C": "11"}')

    executor = make_executor(handler)
    result = await executor.execute(TOOL_NAME, {"city": "London"})

    assert result.status == HTTPStatus.OK
    assert result.body == '{"temp_C": "11"}'
    assert captured[0].method == "GET"
    assert str(captured[0].url) == "https://wttr.in/London?format=j2"
    assert captured[0].headers.get(AUTH_HEADER) is None


async def test_execute_quotes_param_values() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK)

    executor = make_executor(handler)
    await executor.execute(TOOL_NAME, {"city": "New York&co"})

    assert "New%20York%26co" in str(captured[0].url)


async def test_execute_rejects_missing_required_param() -> None:
    executor = make_executor(ok_handler())

    with pytest.raises(ExternalCallError, match="missing"):
        await executor.execute(TOOL_NAME, {})


async def test_execute_rejects_unknown_param() -> None:
    executor = make_executor(ok_handler())

    with pytest.raises(ExternalCallError, match="unknown"):
        await executor.execute(TOOL_NAME, {"city": "London", "extra": "x"})


async def test_execute_allows_skipping_optional_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK)

    executor = make_executor(handler, {INTERNAL_TOOL_NAME: INTERNAL_TOOL_CONTENT})
    result = await executor.execute(INTERNAL_TOOL_NAME, {"service": "billing"})

    assert result.status == HTTPStatus.OK
    assert str(captured[0].url) == "https://api.internal.example.com/v1/status/billing"


async def test_unknown_tool_name_raises_not_found() -> None:
    executor = make_executor(ok_handler())

    with pytest.raises(InstructionNotFoundError):
        await executor.execute("no_such_tool", {})


async def test_invalid_tool_content_raises_spec_error() -> None:
    executor = make_executor(ok_handler(), records={TOOL_NAME: "not json"})

    with pytest.raises(ToolSpecError):
        await executor.execute(TOOL_NAME, {"city": "London"})


async def test_auth_header_added_only_for_whitelisted_prefix() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK)

    whitelist = (
        ExternalCallAuth(
            base_url_prefix=INTERNAL_PREFIX,
            header_name=AUTH_HEADER,
            header_value=AUTH_VALUE,
        ),
    )
    records = {TOOL_NAME: WEATHER_TOOL_CONTENT, INTERNAL_TOOL_NAME: INTERNAL_TOOL_CONTENT}
    executor = make_executor(handler, records=records, whitelist=whitelist)

    await executor.execute(INTERNAL_TOOL_NAME, {"service": "billing"})
    await executor.execute(TOOL_NAME, {"city": "London"})

    internal_request, weather_request = captured
    assert internal_request.headers[AUTH_HEADER] == AUTH_VALUE
    assert weather_request.headers.get(AUTH_HEADER) is None


async def test_ssrf_block_prevents_the_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(HTTPStatus.OK)

    executor = make_executor(handler, ips=(PRIVATE_IP,))

    with pytest.raises(SsrfBlockedError):
        await executor.execute(TOOL_NAME, {"city": "London"})
    assert calls == []


async def test_redirect_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.FOUND,
            headers={"Location": REDIRECT_TARGET},
        )

    executor = make_executor(handler)
    result = await executor.execute(TOOL_NAME, {"city": "London"})

    # the guard never sees the redirect target; the 3xx is returned as data
    assert result.status == HTTPStatus.FOUND


async def test_long_body_is_truncated() -> None:
    executor = make_executor(ok_handler(body="x" * (MAX_BODY_CHARS + 10)))

    result = await executor.execute(TOOL_NAME, {"city": "London"})

    assert result.body.endswith(TRUNCATED_SUFFIX)
    assert len(result.body) == MAX_BODY_CHARS + len(TRUNCATED_SUFFIX)


async def test_upstream_connection_error_raises_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    executor = make_executor(handler)

    with pytest.raises(ExternalCallError):
        await executor.execute(TOOL_NAME, {"city": "London"})


# --- per-user auth templating and the self-API allowlist ----------------------

USER_A = "alice"
USER_ID_HEADER = "X-User-Id"
USER_ID_TEMPLATE = "{user_id}"
SELF_TOOL_NAME = "cron_list_jobs"
SELF_BASE_URL = "http://127.0.0.1:8000"
LOOPBACK_IP = "127.0.0.1"
SELF_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": f"{SELF_BASE_URL}/api/cron/jobs",
        "params_schema": {},
        "auth": "none",
    }
)


def make_self_executor(
    captured: list[httpx.Request],
    header_value: str = USER_ID_TEMPLATE,
) -> ExternalCallExecutor:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="[]")

    return make_executor(
        handler,
        records={SELF_TOOL_NAME: SELF_TOOL_CONTENT},
        ips=(LOOPBACK_IP,),  # would be blocked without the allowlisted prefix
        whitelist=(
            ExternalCallAuth(
                base_url_prefix=SELF_BASE_URL,
                header_name=USER_ID_HEADER,
                header_value=header_value,
            ),
        ),
        allowed_prefixes=(SELF_BASE_URL,),
    )


async def test_user_id_template_is_substituted_into_the_auth_header() -> None:
    captured: list[httpx.Request] = []
    executor = make_self_executor(captured)

    result = await executor.execute(SELF_TOOL_NAME, {}, user_id=USER_A)

    assert result.status == HTTPStatus.OK  # loopback passed the guard via the allowlist
    assert captured[0].headers[USER_ID_HEADER] == USER_A


async def test_user_id_template_without_a_user_sends_no_header() -> None:
    captured: list[httpx.Request] = []
    executor = make_self_executor(captured)

    await executor.execute(SELF_TOOL_NAME, {})

    assert captured[0].headers.get(USER_ID_HEADER) is None


async def test_static_auth_value_is_sent_unchanged_even_with_a_user() -> None:
    captured: list[httpx.Request] = []
    executor = make_self_executor(captured, header_value=AUTH_VALUE)

    await executor.execute(SELF_TOOL_NAME, {}, user_id=USER_A)

    assert captured[0].headers[USER_ID_HEADER] == AUTH_VALUE


async def test_skill_passes_the_context_user_id_to_the_executor() -> None:
    captured: list[httpx.Request] = []
    executor = make_self_executor(captured)
    skill = ExternalCallSkill(executor=executor)
    context = SkillContext(user_id=USER_A, channel="web", dialog_id="dialog-1")

    output = await skill.execute({"name": SELF_TOOL_NAME, "params": {}}, context)

    assert output.startswith(f"HTTP {HTTPStatus.OK}")
    assert captured[0].headers[USER_ID_HEADER] == USER_A
