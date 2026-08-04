"""One-time links binding a web secrets form to a Telegram user.

The T2 ingestion path: the user runs /secrets in Telegram, the bot replies
with a short-lived link to the HTTPS form, and the secret travels straight
from the browser to the backend — it never exists inside a Telegram chat,
a dialog narrative or an LLM prompt.

The token is a capability: whoever holds it may manage that account's
secrets until it expires. It carries its own claim rather than naming a row:
the account it was issued for, encrypted under a key derived from the
installation's secrets key, with Fernet's own timestamp as the expiry.
Nothing is stored anywhere.

It names an **account**, not a person, and the service turns one into the
other when the form arrives. That direction is not a detail. Whoever hands
out the link may not know who the account belongs to — the Telegram
ingestion node has no identity store, only its own invite database — while
the service always does, because it answers `X-User-Id` the same way. Having
the link name a person instead once put the form in a different namespace
from the agent: it wrote secrets under `tg:<account>`, the agent looked for
them under the person, and every secret saved after the identity migration
was invisible with no error anywhere.

That is not a micro-optimization, it is what makes the form work at all once
the service runs on more than one pod. The form has no `X-User-Id` to be
routed by — the token *is* its identity — so a balancer cannot send it back
to whichever process issued it, and a token kept in one process's memory
would be refused on the others with nothing the user could act on. It also
means a restart no longer invalidates a link somebody is about to open, and
that there is no table of pending tokens to bound, purge or replicate.
"""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from octoforge_core.secrets.api import (
    SecretFormPrefill,
    normalize_placements,
    normalize_transform,
)

from octoforge_server.config import Settings

TOKEN_TTL_SECONDS = 600.0
# Domain separation for the two uses of OF_SECRETS_KEY. That key encrypts
# stored secret values; minting capability tokens is a different purpose, and
# deriving a distinct key means neither use can weaken the other.
LINK_KEY_PERSON = b"of-links"


def derive_link_key(secrets_key: str) -> bytes:
    """The Fernet key this service signs links with, from the installation key."""
    digest = hashlib.blake2b(secrets_key.encode(), digest_size=32, person=LINK_KEY_PERSON).digest()
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True, slots=True)
class LinkSubject:
    """The account a link was issued for, as the surface names it.

    Two fields rather than one prefixed string, so nothing downstream is
    tempted to recover the surface by parsing an id. That parsing is what
    hid `admin_manage` and what broke this form.
    """

    surface: str
    external_id: str


@dataclass(frozen=True, slots=True)
class RedeemedLink:
    """What a valid token buys: whose secrets it opens, plus form prefill.

    Exactly one of `subject`/`user_id` is set. A surface that hands out
    links knows only its own account id (`subject`, resolved to a person by
    the service); the agent's `secret_link` tool runs inside the service and
    already knows the person, so its tokens name the `user_id` directly and
    carry the pre-filled fields.
    """

    subject: LinkSubject | None = None
    user_id: str | None = None
    prefill: SecretFormPrefill | None = None


class SecretLinkService:
    """Issues and validates the tokens of the secrets form."""

    def __init__(self, key: str = "", ttl_seconds: float = TOKEN_TTL_SECONDS) -> None:
        # An installation with no secrets key has the whole surface disabled:
        # the store is None and every endpoint answers 503. Tokens are still
        # minted so the code path is uniform, but under a throwaway key — a
        # key derived from the empty string would be a public constant, and
        # tokens forgeable from it are worse than tokens nobody can redeem.
        self._fernet = Fernet(derive_link_key(key) if key else Fernet.generate_key())
        self.ttl_seconds = ttl_seconds

    def issue(self, subject: LinkSubject) -> str:
        """Return a fresh token authorizing secret management for that account."""
        payload = json.dumps({"s": subject.surface, "e": subject.external_id})
        return self._fernet.encrypt(payload.encode()).decode()

    def issue_for_person(self, user_id: str, prefill: SecretFormPrefill) -> str:
        """Return a fresh token for a known person, carrying the form prefill.

        Only code that already resolved the person may mint these (the
        agent's secret_link tool); the prefill rides inside the encrypted
        token, so it is tamper-proof and stays out of query strings and logs
        just like the identity.
        """
        payload = json.dumps({"p": user_id, "f": _prefill_to_payload(prefill)})
        return self._fernet.encrypt(payload.encode()).decode()

    def redeem(self, token: str) -> RedeemedLink | None:
        """Return what a valid token buys; None otherwise.

        Deliberately not single-use: the form makes several calls (list, add,
        delete) within one session; the TTL bounds the exposure instead.

        The TTL is checked against wall-clock time on whichever pod redeems,
        so pods must agree on the time — an hour of skew would expire fresh
        links or honour stale ones. Any NTP-synced host is far inside the
        margin; Fernet additionally refuses a token stamped in the future.
        """
        try:
            raw = self._fernet.decrypt(token.encode(), ttl=int(self.ttl_seconds))
            payload = json.loads(raw)
            if "p" in payload:
                return RedeemedLink(
                    user_id=str(payload["p"]),
                    prefill=_prefill_from_payload(payload.get("f")),
                )
            return RedeemedLink(
                subject=LinkSubject(surface=str(payload["s"]), external_id=str(payload["e"]))
            )
        except (InvalidToken, ValueError, KeyError, TypeError):
            # invalid, expired, not a Fernet token, or a payload this version
            # does not understand — to the caller these are one situation:
            # this token buys nothing
            return None


def secrets_link_builder(
    settings: Settings, links: SecretLinkService, surface: str
) -> Callable[[str], str]:
    """Build the /secrets URL factory, keyed by the surface's own account id.

    Takes an external id — what the surface calls the account — because that
    is all the caller reliably has. The ingestion node in particular knows
    nothing else.

    The token rides in the URL *fragment*, not the query string: a fragment is
    never sent to the server, so it cannot land in an access log (Caddy logs
    the request URI), in a proxy log or in a Referer header. The page reads it
    from `location.hash` and posts it in a request body.

    One home for both processes that hand out this link. The pod builds it
    when it runs the bot itself; the ingestion node builds it when the bot
    lives out there — and the shape of the URL is a security property, not a
    detail either of them may drift on.
    """
    base = settings.resolved_public_base_url()

    def build(external_id: str) -> str:
        token = links.issue(LinkSubject(surface=surface, external_id=external_id))
        return f"{base}/secrets.html#token={token}"

    return build


class PersonSecretsLinkBuilder:
    """`SecretFormLinkFactory` port implementation for the agent's secret_link tool.

    Same URL shape (token in the fragment) and the same reasons as
    `secrets_link_builder`; the difference is the subject — a person the
    service already resolved — and the prefill riding in the token.
    """

    def __init__(self, settings: Settings, links: SecretLinkService) -> None:
        self._base = settings.resolved_public_base_url()
        self._links = links

    def build_prefilled(self, user_id: str, prefill: SecretFormPrefill) -> str:
        """Return a short-lived form URL with everything but the value filled."""
        token = self._links.issue_for_person(user_id, prefill)
        return f"{self._base}/secrets.html#token={token}"


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
