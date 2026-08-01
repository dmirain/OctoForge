"""Settings of the Telegram surface.

Its own class, read from the same environment. What a bot token is, and how
long a voice message may be, is this surface's business — a service that
carried those fields would be describing an interface it does not know it
has, and a deployment without Telegram would still document them.

The variable names are unchanged (`OF_TELEGRAM_*`, `OF_VOICE_MAX_SECONDS`):
where a setting is *read* is an internal matter, and moving it must not make
an operator edit `.env`.
"""

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "OF_"
ENV_FILE = ".env"

DEFAULT_POLL_TIMEOUT_SECONDS = 30.0
DEFAULT_EDIT_THROTTLE_SECONDS = 1.5
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./telegram.db"
DEFAULT_INVITE_TTL_SECONDS = 259200.0  # 3 days
DEFAULT_VOICE_MAX_SECONDS = 600.0

#: Handle decorations an operator might paste instead of a bare name.
HANDLE_PREFIXES = ("https://t.me/", "http://t.me/", "t.me/", "@")


class TelegramSettings(BaseSettings):
    """Environment-driven settings of the Telegram surface.

    An empty token means the surface is not configured, which the deployment
    reads as "not installed here" — the same rule every optional capability
    follows.
    """

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_file=ENV_FILE, extra="ignore")

    telegram_bot_token: str = ""
    telegram_poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS
    telegram_edit_throttle_seconds: float = DEFAULT_EDIT_THROTTLE_SECONDS
    telegram_database_url: str = DEFAULT_DATABASE_URL
    telegram_invite_ttl_seconds: float = DEFAULT_INVITE_TTL_SECONDS
    telegram_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # The bot's public @handle. Only the Bot API knows it, and nothing in the
    # code may assume it: with it configured, an invite is handed out as a
    # t.me deep link instead of a bare code the recipient has to paste.
    telegram_bot_username: str = ""
    # Longest recording accepted: a cap on latency and on the provider's daily
    # audio quota, checked against the duration the update carries.
    voice_max_seconds: float = DEFAULT_VOICE_MAX_SECONDS

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Accept a comma-separated string for the admin id list (`.env` friendly)."""
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    def resolved_bot_username(self) -> str:
        """Bot handle without decoration: `@name`, a t.me URL or a bare name all work."""
        raw = self.telegram_bot_username.strip()
        for prefix in HANDLE_PREFIXES:
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix) :]
                break
        return raw.strip("/").strip()
