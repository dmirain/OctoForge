"""One-time links binding a web secrets form to a Telegram user.

The T2 ingestion path: the user runs /secrets in Telegram, the bot replies
with a short-lived link to the HTTPS form, and the secret travels straight
from the browser to the backend — it never exists inside a Telegram chat,
a dialog narrative or an LLM prompt.

The token is a capability: whoever holds it may manage that user's secrets
until it expires. 128 bits of urlsafe randomness makes guessing infeasible;
the TTL keeps a leaked link (chat forwarding, screen sharing) short-lived.
In-memory on purpose: tokens are ephemeral by design, and losing them on a
restart only means running /secrets again.
"""

import secrets
import time
from dataclasses import dataclass, field

TOKEN_TTL_SECONDS = 600.0
MAX_PENDING_TOKENS = 1000
TOKEN_BYTES = 16


@dataclass(slots=True)
class _Link:
    user_id: str
    expires_at: float


@dataclass(slots=True)
class SecretLinkService:
    """Issues and validates the one-time tokens of the secrets form."""

    ttl_seconds: float = TOKEN_TTL_SECONDS
    _links: dict[str, _Link] = field(default_factory=dict)

    def issue(self, user_id: str) -> str:
        """Return a fresh token authorizing secret management for `user_id`."""
        self._purge()
        if len(self._links) >= MAX_PENDING_TOKENS:
            # a flood of /secrets must not grow memory without bound; dropping
            # the oldest pending link only costs its owner a re-run
            oldest = min(self._links, key=lambda token: self._links[token].expires_at)
            del self._links[oldest]
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._links[token] = _Link(user_id=user_id, expires_at=time.monotonic() + self.ttl_seconds)
        return token

    def redeem(self, token: str) -> str | None:
        """Return the user id a valid token is bound to; None otherwise.

        Deliberately not single-use: the form makes several calls (list, add,
        delete) within one session; the TTL bounds the exposure instead.
        """
        self._purge()
        link = self._links.get(token)
        return link.user_id if link is not None else None

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [token for token, link in self._links.items() if link.expires_at <= now]
        for token in expired:
            del self._links[token]
