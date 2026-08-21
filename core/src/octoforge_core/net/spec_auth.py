"""Authentication declaration parsing for endpoint contracts."""

from octoforge_core.net.errors import ToolSpecError
from octoforge_core.net.spec_templates import REF_CODE_PATTERN, SECRET_NAMESPACE
from octoforge_core.net.spec_types import DEFAULT_AUTH

DEFAULT_SECRET_HEADER = "Authorization"
DEFAULT_SECRET_FORMAT = "Bearer {value}"
SECRET_VALUE_FIELD = "{value}"


def parse_auth(raw: object) -> tuple[str, tuple[str, str] | None]:
    if isinstance(raw, dict):
        return "secret", _expand_secret_auth(raw)
    if not isinstance(raw, str):
        raise ToolSpecError("auth must be a string or a secret declaration object")
    auth = raw.strip().lower()
    if auth != DEFAULT_AUTH:
        raise ToolSpecError(
            f"auth: {raw!r} declares authentication but attaches no credential; "
            'use auth: {"secret": "<code>"} or auth: "none"'
        )
    return auth, None


def _expand_secret_auth(raw: dict[str, object]) -> tuple[str, str]:
    code = raw.get("secret")
    if not isinstance(code, str) or not code.strip():
        raise ToolSpecError("auth.secret must be a non-empty secret code")
    code = code.strip().lower()
    if not REF_CODE_PATTERN.match(code):
        raise ToolSpecError("auth.secret must be 1-64 characters of [a-z0-9_]")
    header = raw.get("header", DEFAULT_SECRET_HEADER)
    if not isinstance(header, str) or not header.strip():
        raise ToolSpecError("auth.header must be a non-empty header name")
    value_format = raw.get("format", DEFAULT_SECRET_FORMAT)
    if not isinstance(value_format, str) or SECRET_VALUE_FIELD not in value_format:
        raise ToolSpecError(f"auth.format must contain the {SECRET_VALUE_FIELD} placeholder")
    return header.strip(), value_format.replace(
        SECRET_VALUE_FIELD,
        f"{{{SECRET_NAMESPACE}.{code}}}",
    )
