"""Metadata-only response serialization for secret form sessions."""

from typing import Any

from octoforge_core.secrets.api import SecretFormPrefill, SecretInfo


def session_response(
    infos: list[SecretInfo],
    prefill: SecretFormPrefill | None,
) -> dict[str, Any]:
    return {
        "secrets": [_secret_info(info) for info in infos],
        "prefill": _prefill(prefill) if prefill is not None else None,
    }


def _secret_info(info: SecretInfo) -> dict[str, object]:
    return {
        "code": info.code,
        "allowed_host": info.allowed_host,
        "description": info.description,
        "placements": sorted(member.value for member in info.placements),
        "transform": info.transform.value if info.transform is not None else None,
        "created_at": info.created_at.isoformat(),
        "last_used_at": info.last_used_at.isoformat() if info.last_used_at is not None else None,
    }


def _prefill(prefill: SecretFormPrefill) -> dict[str, object]:
    return {
        "code": prefill.code,
        "allowed_host": prefill.allowed_host,
        "description": prefill.description,
        "placements": sorted(member.value for member in prefill.placements),
        "transform": prefill.transform.value if prefill.transform is not None else None,
    }
