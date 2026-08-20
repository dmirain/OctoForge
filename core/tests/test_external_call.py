"""Tests for the tool spec parser and the external call executor."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
)
from octoforge_core.net.collections.api import CollectionConfig
from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.net.errors import ExternalCallError, SsrfBlockedError, ToolSpecError
from octoforge_core.net.external import (
    MAX_BODY_CHARS,
    TRUNCATED_SUFFIX,
    CallCredentials,
    CallOptions,
    ExternalCallAuth,
    ExternalCallExecutor,
)
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tool_spec import parse_tool_spec
from octoforge_core.net.tools import EndpointGetTool, ExternalCallTool
from octoforge_core.params.api import UserParam, UserParamStore
from octoforge_core.secrets.api import (
    DEFAULT_PLACEMENTS,
    ResolvedSecret,
    SecretHostMismatchError,
    SecretInfo,
    SecretNotFoundError,
    SecretPlacement,
    SecretStore,
    SecretTransform,
    apply_transform,
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


class FakeUserParamStore:
    """UserParamStore stub with scripted values for one user."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    async def put(self, user_id: str, code: str, value: str) -> UserParam:
        raise NotImplementedError

    async def get_for_user(self, user_id: str) -> dict[str, str]:
        return dict(self._values)

    async def list(self, user_id: str) -> list[UserParam]:
        raise NotImplementedError

    async def delete(self, user_id: str, code: str) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class ExecutorOptions:
    """Optional knobs of make_executor bundled to appease the arg-count limit."""

    records: dict[str, str] | None = None
    ips: tuple[str, ...] = (PUBLIC_IP,)
    whitelist: tuple[ExternalCallAuth, ...] = ()
    allowed_prefixes: tuple[str, ...] = ()
    secrets: SecretStore | None = None
    user_params: UserParamStore | None = None
    spill: ResponseSpill | None = None


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
        credentials=CallCredentials(
            auth_whitelist=resolved.whitelist,
            secrets=resolved.secrets,
            user_params=resolved.user_params,
        ),
        spill=resolved.spill,
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

    def __init__(
        self,
        code: str = SECRET_CODE,
        host: str = SECRET_HOST,
        placements: frozenset[SecretPlacement] = DEFAULT_PLACEMENTS,
        transform: SecretTransform | None = None,
    ) -> None:
        self._code = code
        self._host = host
        self._placements = placements
        self._transform = transform
        self.resolutions: list[tuple[str, str, str]] = []

    async def put(  # noqa: PLR0913, PLR0917 — mirrors the port
        self,
        user_id: str,
        code: str,
        value: str,
        allowed_host: str,
        description: str,
        placements: Iterable[str] = (),
        transform: str | None = None,
    ) -> SecretInfo:
        raise NotImplementedError

    async def list(self, user_id: str) -> list[SecretInfo]:
        raise NotImplementedError

    async def delete(self, user_id: str, code: str) -> None:
        raise NotImplementedError

    async def resolve(self, user_id: str, code: str, host: str) -> ResolvedSecret:
        self.resolutions.append((user_id, code, host))
        if code != self._code:
            raise SecretNotFoundError(code)
        if host != self._host:
            raise SecretHostMismatchError(f"secret '{code}' is bound to '{self._host}'")
        return ResolvedSecret(
            value=apply_transform(SECRET_VALUE, self._transform),
            plain=SECRET_VALUE,
            placements=self._placements,
        )


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


def test_tool_spec_expands_secret_auth_into_a_header_template() -> None:
    spec = parse_tool_spec(
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://api.example.com/x",
                "auth": {"secret": "mail_token"},
            }
        )
    )

    assert spec.headers["Authorization"] == "Bearer {secret.mail_token}"
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


# --- namespaced templates: {user.*} and {secret.*} ---------------------------

PARAMS_TOOL_NAME = "calendar_api"
PARAMS_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": "https://cal.example.com/{user.account_id}/events?tz={user.timezone}",
        "params_schema": {},
        "auth": "none",
    }
)


async def test_user_params_are_substituted_into_the_url() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(
        handler,
        records={PARAMS_TOOL_NAME: PARAMS_TOOL_CONTENT},
        options=ExecutorOptions(
            user_params=FakeUserParamStore({"account_id": "acc-1", "timezone": "Europe/Berlin"})
        ),
    )

    await executor.execute(PARAMS_TOOL_NAME, {}, user_id="user-test")

    assert str(captured[0].url) == "https://cal.example.com/acc-1/events?tz=Europe%2FBerlin"


async def test_missing_user_param_guides_the_agent() -> None:
    executor = make_executor(
        ok_handler(),
        records={PARAMS_TOOL_NAME: PARAMS_TOOL_CONTENT},
        options=ExecutorOptions(user_params=FakeUserParamStore({"account_id": "acc-1"})),
    )

    with pytest.raises(ExternalCallError, match="admin console") as denied:
        await executor.execute(PARAMS_TOOL_NAME, {}, user_id="user-test")

    assert "'timezone'" in str(denied.value)


async def test_user_params_unwired_is_a_clear_error() -> None:
    executor = make_executor(ok_handler(), records={PARAMS_TOOL_NAME: PARAMS_TOOL_CONTENT})

    with pytest.raises(ExternalCallError, match="not wired"):
        await executor.execute(PARAMS_TOOL_NAME, {}, user_id="user-test")


QUERY_SECRET_TOOL_NAME = "query_key_api"
QUERY_SECRET_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": f"https://{SECRET_HOST}/v1/data?api_key={{secret.{SECRET_CODE}}}",
        "params_schema": {},
        "auth": "none",
    }
)


async def test_secret_in_url_requires_the_url_placement() -> None:
    executor = make_executor(
        ok_handler(),
        records={QUERY_SECRET_TOOL_NAME: QUERY_SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=FakeSecretStore()),  # header-only default
    )

    with pytest.raises(ExternalCallError, match="url") as denied:
        await executor.execute(QUERY_SECRET_TOOL_NAME, {}, user_id="user-test")

    assert SECRET_VALUE not in str(denied.value)


async def test_secret_in_url_is_injected_when_the_placement_allows() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(
        handler,
        records={QUERY_SECRET_TOOL_NAME: QUERY_SECRET_TOOL_CONTENT},
        options=ExecutorOptions(
            secrets=FakeSecretStore(
                placements=frozenset({SecretPlacement.HEADER, SecretPlacement.URL})
            )
        ),
    )

    await executor.execute(QUERY_SECRET_TOOL_NAME, {}, user_id="user-test")

    assert captured[0].url.params["api_key"] == SECRET_VALUE


async def test_secret_in_body_is_injected_when_the_placement_allows() -> None:
    content = json.dumps(
        {
            "method": "POST",
            "url_template": f"https://{SECRET_HOST}/v1/rpc",
            "body_template": f'{{{{"token": "{{secret.{SECRET_CODE}}}", "q": "{{query}}"}}}}',
            "params_schema": {"query": {"type": "string", "required": True}},
            "auth": "none",
        }
    )
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(
        handler,
        records={"rpc": content},
        options=ExecutorOptions(
            secrets=FakeSecretStore(
                placements=frozenset({SecretPlacement.HEADER, SecretPlacement.BODY})
            )
        ),
    )

    await executor.execute("rpc", {"query": "hello"}, user_id="user-test")

    assert json.loads(captured[0].content) == {"token": SECRET_VALUE, "q": "hello"}


async def test_transform_applies_before_substitution() -> None:
    """base64 for HTTP Basic: the stored value is user:password."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    content = json.dumps(
        {
            "method": "GET",
            "url_template": f"https://{SECRET_HOST}/v1/inbox",
            "params_schema": {},
            "auth": {"secret": SECRET_CODE, "format": "Basic {value}"},
        }
    )
    executor = make_executor(
        handler,
        records={SECRET_TOOL_NAME: content},
        options=ExecutorOptions(secrets=FakeSecretStore(transform=SecretTransform.BASE64)),
    )

    await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    expected = apply_transform(SECRET_VALUE, SecretTransform.BASE64)
    assert captured[0].headers["Authorization"] == f"Basic {expected}"


async def test_scrub_masks_both_the_sent_and_the_plain_value() -> None:
    """An API that inverts the transform can echo the plain value back."""
    encoded = apply_transform(SECRET_VALUE, SecretTransform.BASE64)
    executor = make_executor(
        lambda request: httpx.Response(
            HTTPStatus.OK, text=f'{{"sent": "{encoded}", "decoded": "{SECRET_VALUE}"}}'
        ),
        records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=FakeSecretStore(transform=SecretTransform.BASE64)),
    )

    result = await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    assert SECRET_VALUE not in result.body
    assert encoded not in result.body


async def test_model_param_in_header_must_render_header_safe() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://api.example.com/v1",
            "headers": {"X-Trace": "{trace}"},
            "params_schema": {"trace": {"type": "string", "required": True}},
            "auth": "none",
        }
    )
    executor = make_executor(ok_handler(), records={"traced": content})

    with pytest.raises(ExternalCallError, match="illegal value"):
        await executor.execute("traced", {"trace": "evil\r\nX-Injected: 1"}, user_id="user-test")


@pytest.mark.parametrize(
    "template",
    [
        "https://api.example.com/{unknown.code}",
        "https://api.example.com/{user.BAD-CODE}",
        "https://api.example.com/{}",
        "https://api.example.com/{city:>10}",
    ],
)
def test_tool_spec_rejects_malformed_placeholders(template: str) -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": template,
            "params_schema": {"city": {"type": "string", "required": True}},
        }
    )

    with pytest.raises(ToolSpecError):
        parse_tool_spec(content)


def test_tool_spec_rejects_a_secret_in_the_url_host() -> None:
    content = json.dumps({"method": "GET", "url_template": "https://{secret.tok}.example.com/x"})

    with pytest.raises(ToolSpecError, match="host"):
        parse_tool_spec(content)


def test_tool_spec_rejects_dotted_param_names() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://api.example.com/x",
            "params_schema": {"user.tz": {"type": "string", "required": True}},
        }
    )

    with pytest.raises(ToolSpecError, match="reserved"):
        parse_tool_spec(content)


def test_tool_spec_rejects_invented_fields() -> None:
    """The shape three production records drifted into: invented field names.

    `secret_key` and `body` were never part of the document; ignoring them
    sent the request with no credential and no body, and the only symptom
    was a bare 401 from the far side.
    """
    content = json.dumps(
        {
            "method": "PROPFIND",
            "url_template": "https://caldav.icloud.com/",
            "auth": "basic",
            "secret_key": "caldavicloud",
            "body": "<D:propfind/>",
        }
    )

    with pytest.raises(ToolSpecError, match="unknown field") as refused:
        parse_tool_spec(content)

    message = str(refused.value)
    assert "secret_key" in message and "body" in message
    assert "body_template" in message  # the error names the real field


def test_tool_spec_rejects_an_auth_word_that_promises_a_credential() -> None:
    content = json.dumps(
        {"method": "GET", "url_template": "https://api.github.com/user", "auth": "bearer"}
    )

    with pytest.raises(ToolSpecError, match="attaches no credential"):
        parse_tool_spec(content)


def test_tool_spec_keeps_free_form_notes() -> None:
    """Documentation next to the contract must not look like a broken feature."""
    spec = parse_tool_spec(
        json.dumps(
            {
                "method": "GET",
                "url_template": "https://api.song.link/v1",
                "auth": "none",
                "notes": "public API, no key needed",
                "description": "resolves a track across services",
            }
        )
    )

    assert spec.auth == "none"


async def test_a_401_without_any_secret_says_no_credential_was_sent() -> None:
    """Otherwise the model reads 401 as "wrong value" and guesses at encodings."""
    content = json.dumps(
        {"method": "GET", "url_template": "https://api.example.com/v1", "auth": "none"}
    )
    executor = make_executor(
        lambda request: httpx.Response(HTTPStatus.UNAUTHORIZED, text="denied"),
        records={"bare": content},
    )

    result = await executor.execute("bare", {}, user_id="user-test")

    assert result.status == HTTPStatus.UNAUTHORIZED
    assert "NO credential" in result.body
    assert "secret_list" in result.body


async def test_a_401_with_a_secret_attached_keeps_the_body_alone() -> None:
    """When a credential WAS sent, 401 means what it says — no hint to add."""
    executor = make_executor(
        lambda request: httpx.Response(HTTPStatus.UNAUTHORIZED, text="denied"),
        records={SECRET_TOOL_NAME: SECRET_TOOL_CONTENT},
        options=ExecutorOptions(secrets=FakeSecretStore()),
    )

    result = await executor.execute(SECRET_TOOL_NAME, {}, user_id="user-test")

    assert "NO credential" not in result.body


# --- path and host parameters ------------------------------------------------

DISCOVERY_TOOL_NAME = "caldav_calendars"
DISCOVERY_TOOL_CONTENT = json.dumps(
    {
        "method": "PROPFIND",
        "url_template": "https://{host}/{calendar_home_path}",
        "params_schema": {
            "host": {"type": "host", "required": True, "hosts": ["*.icloud.com"]},
            "calendar_home_path": {"type": "path", "required": True},
        },
        "auth": "none",
    }
)


async def test_a_path_param_keeps_its_separators() -> None:
    """Escaping them turned `1056456520/principal/` into `...%2Fprincipal%2F`,
    and every CalDAV call after discovery answered 401."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(handler, records={DISCOVERY_TOOL_NAME: DISCOVERY_TOOL_CONTENT})

    await executor.execute(
        DISCOVERY_TOOL_NAME,
        {"host": "p124-caldav.icloud.com", "calendar_home_path": "1056456520/calendars/"},
        user_id="user-test",
    )

    assert str(captured[0].url) == "https://p124-caldav.icloud.com/1056456520/calendars/"


async def test_a_string_param_stays_one_segment() -> None:
    """The default is unchanged: a separator inside a value is data, not structure."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text="{}")

    executor = make_executor(handler, records={TOOL_NAME: WEATHER_TOOL_CONTENT})

    await executor.execute(TOOL_NAME, {"city": "a/b"}, user_id="user-test")

    assert "a%2Fb" in str(captured[0].url)


async def test_a_host_param_is_confined_to_the_records_allowlist() -> None:
    """The record names where it may go; the caller only picks from that."""
    executor = make_executor(ok_handler(), records={DISCOVERY_TOOL_NAME: DISCOVERY_TOOL_CONTENT})

    with pytest.raises(ExternalCallError, match="not one this endpoint may call"):
        await executor.execute(
            DISCOVERY_TOOL_NAME,
            {"host": "evil.example.com", "calendar_home_path": "x/"},
            user_id="user-test",
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"host": "api.icloud.com:443", "calendar_home_path": "x/"}, "bare hostname"),
        ({"host": "user@api.icloud.com", "calendar_home_path": "x/"}, "bare hostname"),
        ({"host": "api.icloud.com", "calendar_home_path": "../../etc/"}, "walk up"),
        ({"host": "api.icloud.com", "calendar_home_path": "https://evil.com/x"}, "not a URL"),
        ({"host": "api.icloud.com", "calendar_home_path": "x/?a=b"}, "not a URL"),
    ],
)
async def test_malformed_path_and_host_values_are_refused(
    params: dict[str, str], message: str
) -> None:
    executor = make_executor(ok_handler(), records={DISCOVERY_TOOL_NAME: DISCOVERY_TOOL_CONTENT})

    with pytest.raises(ExternalCallError, match=message):
        await executor.execute(DISCOVERY_TOOL_NAME, params, user_id="user-test")


def test_only_a_host_param_may_stand_in_the_url_host() -> None:
    """Otherwise the caller, not the record, decides where the request goes."""
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://{anything}/v1",
            "params_schema": {"anything": {"type": "string", "required": True}},
        }
    )

    with pytest.raises(ToolSpecError, match="only a 'host' param"):
        parse_tool_spec(content)


def test_a_host_param_must_declare_its_allowlist() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://{host}/v1",
            "params_schema": {"host": {"type": "host", "required": True}},
        }
    )

    with pytest.raises(ToolSpecError, match="allowlist"):
        parse_tool_spec(content)


def test_an_allowlist_belongs_only_to_a_host_param() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://api.example.com/{path}",
            "params_schema": {
                "path": {"type": "path", "required": True, "hosts": ["*.example.com"]}
            },
        }
    )

    with pytest.raises(ToolSpecError, match="belongs to a 'host' param"):
        parse_tool_spec(content)


def test_an_unknown_param_type_names_the_ones_that_exist() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://api.example.com/x",
            "params_schema": {"n": {"type": "integer", "required": True}},
        }
    )

    with pytest.raises(ToolSpecError, match="string, path, host"):
        parse_tool_spec(content)


# --- the spill: big structured bodies become collections ----------------------


BIG_JSON_BODY = json.dumps({"items": [{"id": index, "t": "x" * 20} for index in range(200)]})


async def test_a_big_json_answer_comes_back_as_a_passport() -> None:
    """The whole point: the model gets the shape and a ref, not 8000 chars."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    spill = ResponseSpill(
        SqlAlchemyCollectionStore(create_session_factory(engine)), CollectionConfig()
    )
    executor = make_executor(ok_handler(BIG_JSON_BODY), options=ExecutorOptions(spill=spill))

    result = await executor.execute(TOOL_NAME, {"city": "London"}, user_id=USER_A)

    assert "col:" in result.body
    assert "200 records" in result.body
    assert "…[truncated]" not in result.body
    await engine.dispose()


async def test_without_a_user_the_spill_stands_down() -> None:
    """No user, no owner: collections are personal, truncation is not."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    spill = ResponseSpill(
        SqlAlchemyCollectionStore(create_session_factory(engine)), CollectionConfig()
    )
    executor = make_executor(ok_handler(BIG_JSON_BODY), options=ExecutorOptions(spill=spill))

    result = await executor.execute(TOOL_NAME, {"city": "London"}, user_id=None)

    assert "col:" not in result.body
    assert result.body.endswith("...[truncated]")
    await engine.dispose()


async def test_a_small_json_answer_stays_inline_with_a_spill_wired() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    spill = ResponseSpill(
        SqlAlchemyCollectionStore(create_session_factory(engine)), CollectionConfig()
    )
    executor = make_executor(ok_handler('{"ok": true}'), options=ExecutorOptions(spill=spill))

    result = await executor.execute(TOOL_NAME, {"city": "London"}, user_id=USER_A)

    assert result.body == '{"ok": true}'
    await engine.dispose()


# --- collect: the pagination walk ----------------------------------------------

PAGED_TOOL_NAME = "crm_contractors"
PAGED_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": "https://crm.test/contractors?page={page}",
        "params_schema": {"page": {"type": "string", "required": True}},
        "auth": "none",
        "response": {"items_path": "data", "fields": {"id": "number", "amount": "number"}},
        "pagination": {"kind": "page", "param": "page", "start": 1, "total_path": "total"},
    }
)
CURSOR_TOOL_NAME = "feed"
CURSOR_TOOL_CONTENT = json.dumps(
    {
        "method": "GET",
        "url_template": "https://feed.test/events?cursor={cursor}",
        "params_schema": {"cursor": {"type": "string", "required": True}},
        "auth": "none",
        "pagination": {"kind": "cursor", "param": "cursor", "cursor_path": "next"},
    }
)
PAGE_SIZE = 2
TOTAL_ITEMS = 5


def _paged_handler(request: httpx.Request) -> httpx.Response:
    """Five items, two per page: pages 1-3 carry data, page 3 is short."""
    page = int(request.url.params.get("page") or 1)
    start = (page - 1) * PAGE_SIZE
    items = [
        {"id": str(index), "amount": f"{index}.5", "noise": "x" * 50}
        for index in range(start, min(start + PAGE_SIZE, TOTAL_ITEMS))
    ]
    return httpx.Response(HTTPStatus.OK, json={"total": TOTAL_ITEMS, "data": items})


async def _sqlite_spill() -> tuple[ResponseSpill, AsyncEngine]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    store = SqlAlchemyCollectionStore(create_session_factory(engine))
    return ResponseSpill(store, CollectionConfig()), engine


async def test_collect_walks_every_page_without_the_model() -> None:
    """One tool call, the whole dataset: the loop is the executor's, not an
    LLM round-trip per page — the thousand-contractors scenario."""
    spill, engine = await _sqlite_spill()
    executor = make_executor(
        _paged_handler,
        records={PAGED_TOOL_NAME: PAGED_TOOL_CONTENT},
        options=ExecutorOptions(spill=spill),
    )

    result = await executor.execute(
        PAGED_TOOL_NAME, {}, user_id=USER_A, options=CallOptions(collect=True)
    )

    assert "col:" in result.body
    assert "5 records" in result.body
    assert "collected 3 page(s)" in result.body
    assert "SOURCE CUT" not in result.body  # the data ended by itself
    ref = result.body.split("col:", 1)[1].split("]", 1)[0]
    passport = await spill.store.passport(USER_A, ref)
    # the declared projection dropped the noise and coerced the strings
    assert set(passport.schema["fields"]) == {"id", "amount"}
    assert passport.schema["fields"]["amount"]["type"] == "number"
    await engine.dispose()


async def test_collect_page_ceiling_marks_the_collection_truncated() -> None:
    """Counts that silently reflect a cap read as the whole truth; say it."""
    spill, engine = await _sqlite_spill()
    executor = make_executor(
        _paged_handler,
        records={PAGED_TOOL_NAME: PAGED_TOOL_CONTENT},
        options=ExecutorOptions(spill=spill),
    )

    result = await executor.execute(
        PAGED_TOOL_NAME, {}, user_id=USER_A, options=CallOptions(collect=True, max_pages=1)
    )

    assert "2 records" in result.body
    assert "SOURCE CUT" in result.body
    await engine.dispose()


async def test_collect_stops_on_a_repeated_cursor() -> None:
    """A server that loops must not loop us; the ceiling is a cost cap, not
    the correctness net."""
    spill, engine = await _sqlite_spill()

    def looping(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, json={"next": "again", "items": [{"id": 1}]})

    executor = make_executor(
        looping,
        records={CURSOR_TOOL_NAME: CURSOR_TOOL_CONTENT},
        options=ExecutorOptions(spill=spill),
    )

    result = await executor.execute(
        CURSOR_TOOL_NAME, {}, user_id=USER_A, options=CallOptions(collect=True)
    )

    # page 1 with cursor "", page 2 with cursor "again", then the repeat stops it
    assert "2 records" in result.body
    await engine.dispose()


async def test_collect_without_a_pagination_section_names_the_remedy() -> None:
    spill, engine = await _sqlite_spill()
    executor = make_executor(ok_handler("{}"), options=ExecutorOptions(spill=spill))

    with pytest.raises(ExternalCallError, match="pagination section"):
        await executor.execute(
            TOOL_NAME, {"city": "London"}, user_id=USER_A, options=CallOptions(collect=True)
        )
    await engine.dispose()


async def test_into_appends_records_from_another_endpoint() -> None:
    """Fetch the contractors, then pour their contacts BESIDE them: the join
    happens in the database, not in the context window."""
    spill, engine = await _sqlite_spill()
    contacts_content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://crm.test/contacts",
            "params_schema": {},
            "auth": "none",
        }
    )

    def contacts_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "crm.test" and request.url.path == "/contacts":
            return httpx.Response(HTTPStatus.OK, json=[{"contractor_id": 1, "email": "a@x"}])
        return _paged_handler(request)

    executor = make_executor(
        contacts_handler,
        records={PAGED_TOOL_NAME: PAGED_TOOL_CONTENT, "crm_contacts": contacts_content},
        options=ExecutorOptions(spill=spill),
    )
    first = await executor.execute(
        PAGED_TOOL_NAME, {}, user_id=USER_A, options=CallOptions(collect=True)
    )
    ref = first.body.split("col:", 1)[1].split("]", 1)[0]

    poured = await executor.execute(
        "crm_contacts", {}, user_id=USER_A, options=CallOptions(into=f"col:{ref}")
    )

    assert f"col:{ref}" in poured.body
    passport = await spill.store.passport(USER_A, ref)
    assert passport.record_count == TOTAL_ITEMS + 1
    assert "email" in passport.schema["fields"]  # the merged schema knows both shapes
    await engine.dispose()


async def test_collect_and_into_are_refused_for_kind_records() -> None:
    spill, engine = await _sqlite_spill()
    mirror_content = json.dumps({"kind": "mcp", "server": "s", "tool": "t", "input_schema": {}})
    executor = make_executor(
        ok_handler(), records={"mirror": mirror_content}, options=ExecutorOptions(spill=spill)
    )

    with pytest.raises(ExternalCallError, match="classic HTTP endpoints only"):
        await executor.execute("mirror", {}, user_id=USER_A, options=CallOptions(collect=True))
    await engine.dispose()


def test_pagination_param_must_be_declared() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://x.test/",
            "params_schema": {},
            "pagination": {"kind": "page", "param": "page"},
        }
    )
    with pytest.raises(ToolSpecError, match="declared parameter"):
        parse_tool_spec(content)


def test_response_fields_reject_an_unknown_type() -> None:
    content = json.dumps(
        {
            "method": "GET",
            "url_template": "https://x.test/",
            "response": {"fields": {"price": "decimal"}},
        }
    )
    with pytest.raises(ToolSpecError, match="allowed: boolean, number, string"):
        parse_tool_spec(content)


async def test_collect_stops_and_says_cut_when_the_byte_quota_fills() -> None:
    """A quota that only create respects is not a quota; the loop keeps what
    fits and the passport admits the cut."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    tight = CollectionConfig(max_bytes_per_user=400)
    spill = ResponseSpill(SqlAlchemyCollectionStore(create_session_factory(engine), tight), tight)
    executor = make_executor(
        _paged_handler,
        records={PAGED_TOOL_NAME: PAGED_TOOL_CONTENT},
        options=ExecutorOptions(spill=spill),
    )

    result = await executor.execute(
        PAGED_TOOL_NAME, {}, user_id=USER_A, options=CallOptions(collect=True)
    )

    assert "SOURCE CUT" in result.body
    # two pages fit under the 400-byte quota, the third was refused
    assert "4 records" in result.body
    await engine.dispose()
