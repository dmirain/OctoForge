"""Parse, validate and render the minimal first-run environment file."""

from dataclasses import dataclass

try:
    from .quickstart_crypto import fernet_key, hash_password, password
    from .quickstart_text import ADMIN_USERNAME, ENV_TEMPLATE, PLACEHOLDER_KEY, REQUIRED
except ImportError:
    from quickstart_crypto import fernet_key, hash_password, password
    from quickstart_text import ADMIN_USERNAME, ENV_TEMPLATE, PLACEHOLDER_KEY, REQUIRED


def read_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def missing_requirements(values: dict[str, str]) -> list[str]:
    return [
        f"  {key} - {why}"
        for key, why in REQUIRED
        if not values.get(key) or values[key] == PLACEHOLDER_KEY
    ]


@dataclass(frozen=True, slots=True)
class EnvValues:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embedding_model: str


def render_env(values: EnvValues) -> tuple[str, str]:
    shown_password = password()
    return ENV_TEMPLATE.format(
        llm_base_url=values.llm_base_url,
        llm_api_key=values.llm_api_key,
        llm_model=values.llm_model,
        embedding_model=values.embedding_model,
        admin_username=ADMIN_USERNAME,
        admin_password_hash=hash_password(shown_password),
        secrets_key=fernet_key(),
    ), shown_password
