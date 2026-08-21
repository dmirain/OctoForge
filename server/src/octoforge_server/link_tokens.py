"""Stateless encrypted capability tokens for the secrets form."""

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from octoforge_core.secrets.api import (
    SecretFormPrefill,
    normalize_placements,
    normalize_transform,
)

LINK_KEY_PERSON = b"of-links"


def derive_link_key(secrets_key: str) -> bytes:
    digest = hashlib.blake2b(
        secrets_key.encode(),
        digest_size=32,
        person=LINK_KEY_PERSON,
    ).digest()
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True, slots=True)
class LinkSubject:
    surface: str
    external_id: str


@dataclass(frozen=True, slots=True)
class RedeemedLink:
    subject: LinkSubject | None = None
    user_id: str | None = None
    prefill: SecretFormPrefill | None = None


class LinkTokenCodec:
    """Encrypt account/person claims and redeem them under a caller-supplied TTL."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(derive_link_key(key) if key else Fernet.generate_key())

    def issue(self, subject: LinkSubject) -> str:
        payload = json.dumps({"s": subject.surface, "e": subject.external_id})
        return self._fernet.encrypt(payload.encode()).decode()

    def issue_for_person(self, user_id: str, prefill: SecretFormPrefill) -> str:
        payload = json.dumps({"p": user_id, "f": _prefill_to_payload(prefill)})
        return self._fernet.encrypt(payload.encode()).decode()

    def redeem(self, token: str, ttl_seconds: float) -> RedeemedLink | None:
        try:
            raw = self._fernet.decrypt(token.encode(), ttl=int(ttl_seconds))
            payload = json.loads(raw)
            if "p" in payload:
                return RedeemedLink(
                    user_id=str(payload["p"]),
                    prefill=_prefill_from_payload(payload.get("f")),
                )
            return RedeemedLink(LinkSubject(str(payload["s"]), str(payload["e"])))
        except (InvalidToken, ValueError, KeyError, TypeError):
            return None

    def existed(self, token: str) -> bool:
        try:
            self._fernet.decrypt(token.encode())
        except (InvalidToken, ValueError):
            return False
        return True


def _prefill_to_payload(prefill: SecretFormPrefill) -> dict[str, object]:
    return {
        "c": prefill.code,
        "h": prefill.allowed_host,
        "d": prefill.description,
        "pl": sorted(member.value for member in prefill.placements),
        "t": prefill.transform.value if prefill.transform is not None else None,
    }


def _prefill_from_payload(raw: object) -> SecretFormPrefill | None:
    if not isinstance(raw, dict):
        return None
    placements = raw.get("pl")
    transform = raw.get("t")
    return SecretFormPrefill(
        code=str(raw["c"]),
        allowed_host=str(raw["h"]),
        description=str(raw["d"]),
        placements=normalize_placements(
            [str(item) for item in placements] if isinstance(placements, list) else []
        ),
        transform=normalize_transform(str(transform) if transform is not None else None),
    )
