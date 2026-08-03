"""Tests for the tool spec parser and the external call executor."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    CallCredentials,
    ExternalCallAuth,
    ExternalCallExecutor,
)
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tool_spec import parse_tool_spec
from octoforge_core.net.tools import EndpointGetTool, ExternalCallTool
from octoforge_core.secrets.api import (
    SecretHostMismatchError,
    SecretNotFoundError,
    SecretStore,
)
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

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
CALDAV_TOOL_NAME = "caldav_query"
CALDAV_TOOL_CONTENT = json.dumps(
    {
        "method": "REPORT",
        "url_template": "https://cal.example.com/dav/{calendar}/",
        "body_template": '<c:calendar-query><c:time-range start="{start}"/></c:calendar-query>',
        "headers": {"Depth": "1", "Content-Type": "application/xml"},
        "params_schema": {
            "calendar": {"type": "string", "required": True},
            "start": {"type": "string", "required": True},
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
        self.get_by_name_calls: list[tuple[str, InstructionType | None, str | None]] = []

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        self.get_by_name_calls.append((name, kind, user_id))
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

    async def search(
        self,
        user_id: str,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError

    async def save(
        self,
        user_id: str,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        raise NotImplementedError


@dataclass(frozen=True)
class ExecutorOptions:
    """Optional knobs of make_executor bundled to appease the arg-count limit."""

    records: dict[str, str] | None = None
    ips: tuple[str, ...] = (PUBLIC_IP,)
    whitelist: tuple[ExternalCallAuth, ...] = ()
    allowed_prefixes: tuple[str, ...] = ()
    secrets: SecretStore | None = None


def make_executor(
    handler: Callable[[httpx.Request], httpx.Response],
    records: dict[str, str] | None = None,
    options: ExecutorOptions | None = None,
) -> ExternalCallExecutor:
    resolved = options if options is not None else ExecutorOptions()
    if records is not None:
        resolved = replace(resolved, records=records)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExternalCallExecutor(
        service=FakeInstructionService(resolved.records or {TOOL_NAME: WEATHER_TOOL_CONTENT}),
        http_client=http_client,
        guard=SsrfGuard(
            resolver=StubResolver(resolved.ips), allowed_prefixes=resolved.allowed_prefixes
        ),
        credentials=CallCredentials(auth_whitelist=resolved.whitelist, secrets=resolved.secrets),
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
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://x.test/{city}",
                # a template field must be required; an optional one would
                # crash the render with a raw KeyError when omitted
                "params_schema": {"city": {"type": "string", "required": False}},
            }
        ),
    ],
)
def test_parse_invalid_specs_raise(content: str) -> None:
    with pytest.raises(ToolSpecError):
        parse_tool_spec(content)


def test_parse_allows_optional_params_outside_the_template() -> None:
    spec = parse_tool_spec(INTERNAL_TOOL_CONTENT)

    assert spec.params["verbose"].required is False


def test_parse_webdav_spec_with_body_and_headers() -> None:
    """CalDAV lives on WebDAV verbs plus an XML body and a Depth header."""
    spec = parse_tool_spec(CALDAV_TOOL_CONTENT)

    assert spec.method == "REPORT"
    assert spec.body_template is not None and "{start}" in spec.body_template
    assert spec.headers == {"Depth": "1", "Content-Type": "application/xml"}


@pytest.mark.parametrize(
    "extra",
    [
        {"body_template": ""},
        {"body_template": ["<xml/>"]},
        {"body_template": "<q>{undeclared}</q>"},
        {"headers": {"Depth": 1}},
        {"headers": {"": "1"}},
        {"headers": ["Depth: 1"]},
    ],
)
def test_parse_rejects_malformed_body_and_headers(extra: dict[str, object]) -> None:
    content = json.dumps({"method": "REPORT", "url_template": "https://x.test/", **extra})

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


async def test_execute_sends_the_rendered_body_and_declared_headers() -> None:
    """The CalDAV shape: a WebDAV verb, an XML body, a Depth header. Body
    values go in verbatim — URL-escaping would corrupt the payload."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.MULTI_STATUS, text="<multistatus/>")

    executor = make_executor(handler, {CALDAV_TOOL_NAME: CALDAV_TOOL_CONTENT})
    result = await executor.execute(
        CALDAV_TOOL_NAME, {"calendar": "work", "start": "20260801T000000Z"}
    )

    assert result.status == HTTPStatus.MULTI_STATUS
    (request,) = captured
    assert request.method == "REPORT"
    assert str(request.url) == "https://cal.example.com/dav/work/"
    assert request.content == (
        b'<c:calendar-query><c:time-range start="20260801T000000Z"/></c:calendar-query>'
    )
    assert request.headers["Depth"] == "1"
    assert request.headers["Content-Type"] == "application/xml"


async def test_a_record_without_a_body_template_sends_no_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK)

    executor = make_executor(handler)
    await executor.execute(TOOL_NAME, {"city": "London"})

    assert captured[0].content == b""


async def test_execute_rejects_missing_required_param() -> None:
    executor = make_executor(ok_handler())

    with pytest.raises(ExternalCallError, match="missing"):
        await executor.execute(TOOL_NAME, {})


async def test_param_errors_carry_the_declared_contract() -> None:
    """A blind call must self-correct in one step, not retry guessed variants."""
    executor = make_executor(ok_handler())

    with pytest.raises(ExternalCallError, match="declares this contract") as missing:
        await executor.execute(TOOL_NAME, {})
    with pytest.raises(ExternalCallError, match="declares this contract") as unknown:
        await executor.execute(TOOL_NAME, {"city": "London", "extra": "x"})

    assert "url_template" in str(missing.value)
    assert "url_template" in str(unknown.value)


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


async def test_executor_passes_the_user_id_to_the_service_lookup() -> None:
    service = FakeInstructionService({TOOL_NAME: WEATHER_TOOL_CONTENT})
    executor = ExternalCallExecutor(
        service=service,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(ok_handler())),
        guard=SsrfGuard(resolver=StubResolver((PUBLIC_IP,))),
    )

    await executor.execute(TOOL_NAME, {"city": "London"}, user_id="user-test")

    assert service.get_by_name_calls == [(TOOL_NAME, InstructionType.ENDPOINT, "user-test")]


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
    executor = make_executor(handler, records=records, options=ExecutorOptions(whitelist=whitelist))

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

    executor = make_executor(handler, options=ExecutorOptions(ips=(PRIVATE_IP,)))

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
        options=ExecutorOptions(
            ips=(LOOPBACK_IP,),  # would be blocked without the allowlisted prefix
            whitelist=(
                ExternalCallAuth(
                    base_url_prefix=SELF_BASE_URL,
                    header_name=USER_ID_HEADER,
                    header_value=header_value,
                ),
            ),
            allowed_prefixes=(SELF_BASE_URL,),
        ),
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
    tool = ExternalCallTool(executor=executor)
    context = ToolContext(user_id=USER_A, channel="web", dialog_id="dialog-1")

    output = await tool.execute({"name": SELF_TOOL_NAME, "params": {}}, context)

    assert output.startswith(f"HTTP {HTTPStatus.OK}")
    assert captured[0].headers[USER_ID_HEADER] == USER_A


SPOOF_TOOL_NAME = "spoofed_self"
SPOOFED_HOST = "public.example.com"
SPOOF_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        # userinfo-spoofs the allowlisted origin; the real host is SPOOFED_HOST
        "url_template": f"{SELF_BASE_URL}@{SPOOFED_HOST}/api/cron/jobs",
        "params_schema": {},
        "auth": "none",
    }
)


async def test_auth_header_is_not_sent_to_a_userinfo_spoofed_origin() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="[]")

    executor = make_executor(
        handler,
        records={SPOOF_TOOL_NAME: SPOOF_TOOL_CONTENT},
        options=ExecutorOptions(
            ips=(PUBLIC_IP,),
            whitelist=(
                ExternalCallAuth(
                    base_url_prefix=SELF_BASE_URL,
                    header_name=USER_ID_HEADER,
                    header_value=USER_ID_TEMPLATE,
                ),
            ),
            allowed_prefixes=(SELF_BASE_URL,),
        ),
    )

    result = await executor.execute(SPOOF_TOOL_NAME, {}, user_id=USER_A)

    assert result.status == HTTPStatus.OK  # a public host, so the request went out
    assert captured[0].url.host == SPOOFED_HOST
    assert captured[0].headers.get(USER_ID_HEADER) is None


# --- endpoint_get -----------------------------------------------------------

ENDPOINT_GET_CONTEXT = ToolContext(user_id="user-test", channel="web", dialog_id="dlg")


def test_endpoint_get_spec() -> None:
    tool = EndpointGetTool(service=FakeInstructionService({}))

    assert tool.spec.name == "endpoint_get"
    assert tool.spec.parameters_schema["required"] == ["name"]


async def test_endpoint_get_returns_the_contract() -> None:
    """The late-binding step: a skill names the endpoint, this resolves its contract."""
    service = FakeInstructionService({TOOL_NAME: WEATHER_TOOL_CONTENT})
    tool = EndpointGetTool(service=service)

    output = await tool.execute({"name": TOOL_NAME}, ENDPOINT_GET_CONTEXT)

    assert output.startswith(f"[endpoint] {TOOL_NAME}")
    assert "url_template" in output
    # the lookup is visibility-scoped: the caller's own endpoints resolve too
    assert service.get_by_name_calls == [(TOOL_NAME, InstructionType.ENDPOINT, "user-test")]


async def test_endpoint_get_not_found_points_to_discovery() -> None:
    tool = EndpointGetTool(service=FakeInstructionService({}))

    output = await tool.execute({"name": "nope"}, ENDPOINT_GET_CONTEXT)

    assert output == (
        "endpoint 'nope' not found; discover endpoints with recall(type=endpoint, query=...)"
    )


async def test_endpoint_get_rejects_invalid_arguments() -> None:
    tool = EndpointGetTool(service=FakeInstructionService({}))

    with pytest.raises(ToolArgumentsError):
        await tool.execute({}, ENDPOINT_GET_CONTEXT)
    with pytest.raises(ToolArgumentsError):
        await tool.execute({"name": "  "}, ENDPOINT_GET_CONTEXT)


# --- per-user secret injection ----------------------------------------------

SECRET_TOOL_NAME = "mail_api"
SECRET_CODE = "mail_token"
SECRET_VALUE = "tok-very-secret-12345"
SECRET_HOST = "api.mail.example.com"
SECRET_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": f"https://{SECRET_HOST}/v1/inbox",
        "params_schema": {},
        "auth": {"secret": SECRET_CODE, "header": "X-Api-Key", "format": "Key {value}"},
    }
)


class FakeSecretStore:
    """SecretStore stub with one scripted secret and recorded resolutions."""

    def __init__(self, code: str = SECRET_CODE, host: str = SECRET_HOST) -> None:
        self._code = code
        self._host = host
        self.resolutions: list[tuple[str, str, str]] = []

    async def put(self, user_id: str, code: str, value: str, allowed_host: str) -> object:
        raise NotImplementedError

    async def list(self, user_id: str) -> list[object]:
        raise NotImplementedError

    async def delete(self, user_id: str, code: str) -> None:
        raise NotImplementedError

    async def resolve(self, user_id: str, code: str, host: str) -> str:
        self.resolutions.append((user_id, code, host))
        if code != self._code:
            raise SecretNotFoundError(code)
        if host != self._host:
            raise SecretHostMismatchError(f"secret '{code}' is bound to '{self._host}'")
        return SECRET_VALUE


async def test_secret_is_injected_as_the_declared_header() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    store = FakeSecretStore()
    executor = make_executor(
        handler,
        records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=store),
    )

    await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    assert captured[0].headers["X-Api-Key"] == f"Key {SECRET_VALUE}"
    assert store.resolutions == [("user-test", SECRET_CODE, SECRET_HOST)]


async def test_a_record_header_never_shadows_the_secret_header() -> None:
    """A poisoned record declaring the secret's own header name must lose:
    credential sources are applied after the record's static headers."""
    content = json.dumps(
        {
            "method": "GET",
            "url_template": f"https://{SECRET_HOST}/v1/inbox",
            "headers": {"X-Api-Key": "record-supplied"},
            "params_schema": {},
            "auth": {"secret": SECRET_CODE, "header": "X-Api-Key", "format": "Key {value}"},
        }
    )
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(
        handler,
        records={SECRET_TOOL_NAME: content},
        options=ExecutorOptions(secrets=FakeSecretStore()),
    )

    await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    assert captured[0].headers["X-Api-Key"] == f"Key {SECRET_VALUE}"


async def test_secret_echo_is_scrubbed_from_the_response() -> None:
    """Echo endpoints reflect request headers; the LLM must never see the value."""
    executor = make_executor(
        lambda request: httpx.Response(
            HTTPStatus.OK, text=f'{{"headers": {{"X-Api-Key": "Key {SECRET_VALUE}"}}}}'
        ),
        records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=FakeSecretStore()),
    )

    result = await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    assert SECRET_VALUE not in result.body
    assert "[secret]" in result.body


async def test_missing_secret_guides_the_agent() -> None:
    executor = make_executor(
        ok_handler(),
        records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=FakeSecretStore(code="other_code")),
    )

    with pytest.raises(ExternalCallError, match="/secrets"):
        await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")


async def test_secrets_disabled_is_a_clear_error() -> None:
    executor = make_executor(ok_handler(), records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT})

    with pytest.raises(ExternalCallError, match="not configured"):
        await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")


async def test_host_mismatch_error_reaches_the_agent_without_the_value() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://another.example.com/v1",
            "params_schema": {},
            "auth": {"secret": SECRET_CODE},
        }
    )
    executor = make_executor(
        ok_handler(),
        records={"other_api": content},
        options=ExecutorOptions(secrets=FakeSecretStore()),
    )

    with pytest.raises(ExternalCallError, match="bound to") as denied:
        await executor.execute("other_api", {}, user_id="user-test")

    assert SECRET_VALUE not in str(denied.value)


def test_tool_spec_parses_secret_auth_with_defaults() -> None:
    spec = parse_tool_spec(
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://api.example.com/x",
                "auth": {"secret": "mail_token"},
            }
        )
    )

    assert spec.secret_auth is not None
    assert spec.secret_auth.code == "mail_token"
    assert spec.secret_auth.header == "Authorization"
    assert spec.secret_auth.format == "Bearer {value}"
    assert spec.auth == "secret"


@pytest.mark.parametrize(
    "auth",
    [
        {"secret": ""},
        {"secret": "x", "header": ""},
        {"secret": "x", "format": "no placeholder"},
        42,
    ],
)
def test_tool_spec_rejects_malformed_secret_auth(auth: object) -> None:
    content = json.dumps(
        {"method": "GET", "url_template": "https://api.example.com/x", "auth": auth}
    )

    with pytest.raises(ToolSpecError):
        parse_tool_spec(content)
