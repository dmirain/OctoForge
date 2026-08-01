"""One-time links binding a web secrets form to a Telegram user.

The T2 ingestion path: the user runs /secrets in Telegram, the bot replies
with a short-lived link to the HTTPS form, and the secret travels straight
from the browser to the backend — it never exists inside a Telegram chat,
a dialog narrative or an LLM prompt.

The token is a capability: whoever holds it may manage that user's secrets
until it expires. It carries its own claim rather than naming a row: the
user id encrypted under a key derived from the installation's secrets key,
with Fernet's own timestamp as the expiry. Nothing is stored anywhere.

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
from collections.abc import Callable

from cryptography.fernet import Fernet, InvalidToken

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

    def issue(self, user_id: str) -> str:
        """Return a fresh token authorizing secret management for `user_id`."""
        return self._fernet.encrypt(user_id.encode()).decode()

    def redeem(self, token: str) -> str | None:
        """Return the user id a valid token is bound to; None otherwise.

        Deliberately not single-use: the form makes several calls (list, add,
        delete) within one session; the TTL bounds the exposure instead.

        The TTL is checked against wall-clock time on whichever pod redeems,
        so pods must agree on the time — an hour of skew would expire fresh
        links or honour stale ones. Any NTP-synced host is far inside the
        margin; Fernet additionally refuses a token stamped in the future.
        """
        try:
            return self._fernet.decrypt(token.encode(), ttl=int(self.ttl_seconds)).decode()
        except (InvalidToken, ValueError):
            # invalid, expired, or not a Fernet token at all — to the caller
            # these are one situation: this token buys nothing
            return None


def secrets_link_builder(settings: Settings, links: SecretLinkService) -> Callable[[str], str]:
    """Build the /secrets URL factory: a fresh token per request.

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

    def build(user_id: str) -> str:
        return f"{base}/secrets.html#token={links.issue(user_id)}"

    return build
