"""Public boundary of the secrets module.

Per-user secrets (API tokens and the like) that must never reach the LLM
context, the message archive or the logs. The rest of the system knows only
secret *codes*: an endpoint record declares which code it needs, and the call
executor resolves the value at request time — nothing else ever reads it.
The store DTO deliberately carries no value field: every listing surface
(web form, admin, tools) is metadata-only by construction.

A secret also carries:

- a required `description` — what the value is for, written by the person who
  stored it; it is what lets the model tell two secrets for one host apart;
- `placements` — the request parts a record template may substitute it into
  (`header` is the only default; `url` and `body` are opt-in, because a URL
  leaks into the remote side's logs and history);
- an optional `transform` — a static function applied to the value before
  substitution (e.g. `base64` for HTTP Basic, where the stored value is
  `user:password`). Dynamic schemes (request signing, OAuth refresh) are
  deliberately out of scope: they are code, not a value filter.
"""

import base64
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_DESCRIPTION_CHARS = 256
WILDCARD_PREFIX = "*."
# what follows `*.` must have a dot of its own: '*.com' would be every host
MIN_PATTERN_SUFFIX_DOTS = 1


class SecretNotFoundError(Exception):
    """Raised when no secret matches the requested (user, code) pair."""


class SecretHostMismatchError(Exception):
    """Raised when a secret is asked for a host it is not bound to.

    The host binding is the exfiltration guard: even a poisoned endpoint
    record (or a typo in one) cannot send a secret anywhere but the host it
    was created for.
    """


class InvalidSecretError(Exception):
    """Raised when a secret being stored is malformed (code, value or host)."""


class SecretPlacement(StrEnum):
    """A request part a secret value may be substituted into."""

    HEADER = "header"
    URL = "url"
    BODY = "body"


# Headers only unless the person storing the secret opted into more: a URL
# carries the value into the remote side's access logs and the browser-ish
# parts of the world, a body into whatever the endpoint does with payloads.
DEFAULT_PLACEMENTS: frozenset[SecretPlacement] = frozenset({SecretPlacement.HEADER})


class SecretTransform(StrEnum):
    """A static transform applied to the stored value before substitution.

    Every member is a pure function of the value alone — anything needing
    per-request inputs (timestamps, nonces, request signing) is not a
    transform and does not belong here.
    """

    BASE64 = "base64"
    BASE64URL = "base64url"
    MD5_HEX = "md5_hex"
    SHA1_HEX = "sha1_hex"
    SHA256_HEX = "sha256_hex"


def apply_transform(value: str, transform: SecretTransform | None) -> str:
    """Return the value as it must appear in the request."""
    match transform:
        case None:
            return value
        case SecretTransform.BASE64:
            return base64.b64encode(value.encode()).decode()
        case SecretTransform.BASE64URL:
            return base64.urlsafe_b64encode(value.encode()).decode()
        case SecretTransform.MD5_HEX:
            return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()
        case SecretTransform.SHA1_HEX:
            return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()
        case SecretTransform.SHA256_HEX:
            return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """Metadata of one stored secret; the value is intentionally absent.

    `description` is always present: the store requires it, and rows from
    before the requirement were backfilled by migration `a1c8e5f3b972` with
    a placeholder that tells the agent to ask the user.
    """

    code: str
    allowed_host: str
    description: str
    placements: frozenset[SecretPlacement]
    transform: SecretTransform | None
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """A secret the executor is about to substitute into one request.

    `value` is what goes into the request (transform already applied);
    `plain` is the stored value, carried ONLY so response scrubbing can mask
    both forms — an API that inverts the transform (Basic auth decodes the
    base64) could otherwise echo the plain value back into model context.
    """

    value: str
    plain: str
    placements: frozenset[SecretPlacement]


def normalize_code(raw: str) -> str:
    """Validate and normalize a secret code (snake_case, ≤64 chars)."""
    code = raw.strip().lower()
    if not CODE_PATTERN.match(code):
        raise InvalidSecretError(
            "secret code must be 1-64 characters of [a-z0-9_], e.g. 'gmail_token'"
        )
    return code


def normalize_host(raw: str) -> str:
    """Validate and normalize the host binding: a hostname or a `*.` pattern.

    A pattern covers services that shard across sibling hosts — iCloud hands
    out `p54-caldav.icloud.com` after discovery, S3 answers on
    `<bucket>.s3.amazonaws.com` — where an exact binding would mean one
    stored copy of the same credential per shard.

    Deliberately narrow, because this binding is the exfiltration guard:

    - only a leading `*.`, never mid-label (`p*.icloud.com`) and never bare;
    - it stands for exactly ONE label, as in TLS certificates, so
      `*.icloud.com` covers `caldav.icloud.com` but not `a.b.icloud.com`;
    - what follows must itself have at least two labels, so `*.com` — every
      host on the internet — cannot be written at all.

    What it cannot know is where a registry boundary sits: `*.co.uk` passes
    the two-label rule and is far broader than it looks. That is why the
    surfaces spell out what a pattern grants.
    """
    host = raw.strip().lower().rstrip(".")
    if not host or "/" in host or ":" in host or " " in host:
        raise InvalidSecretError(
            "allowed_host must be a bare hostname ('api.example.com') or a "
            "one-level pattern ('*.example.com')"
        )
    if not host.startswith(WILDCARD_PREFIX):
        if "*" in host:
            raise InvalidSecretError(
                "a wildcard is only allowed as the leading label, e.g. '*.example.com'"
            )
        return host
    suffix = host[len(WILDCARD_PREFIX) :]
    if "*" in suffix:
        raise InvalidSecretError("only one wildcard label is allowed, e.g. '*.example.com'")
    if suffix.count(".") < MIN_PATTERN_SUFFIX_DOTS or "" in suffix.split("."):
        raise InvalidSecretError(
            f"'{host}' is too broad: a pattern must name at least a domain and a "
            "suffix, e.g. '*.example.com'"
        )
    return host


def host_matches(binding: str, host: str) -> bool:
    """Whether a request host is covered by a secret's binding.

    Exact bindings compare equal; a `*.` pattern replaces exactly one label
    (never the apex, never two), which is the TLS wildcard rule.
    """
    target = host.strip().lower().rstrip(".")
    if "*" in target:
        # a request host is never a pattern; a URL literally containing one
        # must not be able to satisfy a pattern binding
        return False
    if not binding.startswith(WILDCARD_PREFIX):
        return binding == target
    suffix = binding[len(WILDCARD_PREFIX) :]
    if not target.endswith(f".{suffix}"):
        return False
    label = target[: -(len(suffix) + 1)]
    return bool(label) and "." not in label


def normalize_description(raw: str) -> str:
    """Validate the required human/LLM-facing purpose of a secret."""
    description = " ".join(raw.split())
    if not description:
        raise InvalidSecretError(
            "description is required: say what the secret is for, e.g. "
            "'read-only token for the work calendar'"
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise InvalidSecretError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters")
    return description


def normalize_placements(raw: Iterable[str]) -> frozenset[SecretPlacement]:
    """Validate a placements selection; empty means the header-only default."""
    placements = set()
    for item in raw:
        try:
            placements.add(SecretPlacement(str(item).strip().lower()))
        except ValueError:
            allowed = ", ".join(member.value for member in SecretPlacement)
            raise InvalidSecretError(f"unknown placement {item!r}; allowed: {allowed}") from None
    return frozenset(placements) if placements else DEFAULT_PLACEMENTS


def normalize_transform(raw: str | None) -> SecretTransform | None:
    """Validate a transform selection; None or empty means no transform."""
    if raw is None or not str(raw).strip():
        return None
    try:
        return SecretTransform(str(raw).strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in SecretTransform)
        raise InvalidSecretError(f"unknown transform {raw!r}; allowed: {allowed}") from None


@dataclass(frozen=True, slots=True)
class SecretFormPrefill:
    """Everything of one secret except the value, for a pre-filled form link.

    The agent fills these from the failing endpoint's contract; the user
    opens the link and pastes only the value.
    """

    code: str
    allowed_host: str
    description: str
    placements: frozenset[SecretPlacement] = DEFAULT_PLACEMENTS
    transform: SecretTransform | None = None


class SecretFormLinkFactory(Protocol):
    """Mints one-time secrets-form URLs bound to a person.

    Implemented by the composition root's web layer (the link embeds the
    installation's public base URL and a capability code); core only knows
    the port, so the secret_link tool exists exactly when a web surface does.
    """

    async def build_prefilled(self, user_id: str, prefill: SecretFormPrefill) -> str:
        """Return a short-lived form URL with everything but the value filled."""
        ...


@dataclass(frozen=True, slots=True)
class SecretFormSession:
    """What a redeemed form code opens: whose secrets, and any prefill."""

    user_id: str
    prefill: SecretFormPrefill | None = None


class SecretFormLinkStore(Protocol):
    """Short capability codes for the secrets form, with their payload.

    The code is what a person (or an agent) has to carry into a chat, so it
    is deliberately short; everything else about the link lives here. Codes
    are opaque and unguessable — a code IS the authorization to manage that
    person's secrets until it expires.
    """

    async def issue(
        self, user_id: str, prefill: SecretFormPrefill | None, ttl_seconds: float
    ) -> str:
        """Store a fresh code for that person and return it."""
        ...

    async def redeem(self, code: str) -> SecretFormSession | None:
        """Return what a live code opens; None when unknown or expired."""
        ...

    async def is_expired(self, code: str) -> bool:
        """Whether this code existed and has run out — for an honest message."""
        ...


class SecretStore(Protocol):
    """Port of the secrets module: encrypted at rest, value readable only via resolve."""

    async def put(  # noqa: PLR0913, PLR0917 — the full shape of one stored secret
        self,
        user_id: str,
        code: str,
        value: str,
        allowed_host: str,
        description: str,
        placements: Iterable[str] = (),
        transform: str | None = None,
    ) -> SecretInfo:
        """Store or replace the user's secret under `code`, bound to `allowed_host`."""
        ...

    async def list(self, user_id: str) -> list[SecretInfo]:
        """Return the user's secrets metadata, newest first. Never the values."""
        ...

    async def delete(self, user_id: str, code: str) -> None:
        """Delete the user's secret by code; raise `SecretNotFoundError`."""
        ...

    async def resolve(self, user_id: str, code: str, host: str) -> ResolvedSecret:
        """Return the value for a request going to `host`, transform applied.

        The single place a value leaves the store. Raises
        `SecretNotFoundError` for an unknown code and
        `SecretHostMismatchError` when `host` differs from the binding
        (the error text never contains the value). Stamps `last_used_at`.
        Placement enforcement is the caller's: the store does not know which
        request part the value is headed for.
        """
        ...
