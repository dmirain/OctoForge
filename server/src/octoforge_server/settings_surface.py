"""Operator, surface, prompt, egress and logging settings."""

from pathlib import Path
from typing import Annotated

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, SYSTEM_PROMPT_NAME
from octoforge_core.net.external import ExternalCallAuth
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import NoDecode

from octoforge_server.skill_overlay import parse_overlay_source

FILE_SCHEME_PREFIX = "file:"


class ExternalCallAuthSettings(BaseModel):
    base_url_prefix: str
    header_name: str
    header_value: str


class SurfaceSettings(BaseModel):
    service_username: str = ""
    service_password_hash: str = ""
    service_password: str = ""
    self_base_url: str = "http://127.0.0.1:8000"
    public_base_url: str = ""
    http_request_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    external_call_auth_whitelist: list[ExternalCallAuthSettings] = Field(default_factory=list)
    serper_token: str = ""
    system_prompt_source: str = ""
    router_prompt_source: str = ""
    system_skills_source: str = ""
    admin_username: str = "admin"
    admin_password_hash: str = ""
    secrets_key: str = ""
    log_dir: str = ""
    log_max_mb: int = 200
    log_backups: int = 9

    def resolved_public_base_url(self) -> str:
        return (self.public_base_url or self.self_base_url).rstrip("/")

    @field_validator("http_request_allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def to_external_call_auth_whitelist(self) -> tuple[ExternalCallAuth, ...]:
        return tuple(
            ExternalCallAuth(entry.base_url_prefix, entry.header_name, entry.header_value)
            for entry in self.external_call_auth_whitelist
        )

    def to_skills_overlay_path(self) -> Path | None:
        return (
            parse_overlay_source(self.system_skills_source) if self.system_skills_source else None
        )

    def to_prompt_files(self) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for name, source in (
            (SYSTEM_PROMPT_NAME, self.system_prompt_source),
            (ROUTER_PROMPT_NAME, self.router_prompt_source),
        ):
            if source:
                files[name] = _parse_prompt_source(source)
        return files


def _parse_prompt_source(source: str) -> Path:
    if not source.startswith(FILE_SCHEME_PREFIX):
        raise ValueError(f"unsupported prompt source {source!r}: only 'file:' is supported")
    return Path(source.removeprefix(FILE_SCHEME_PREFIX))
