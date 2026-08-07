"""How ingestion hands a message to the dialog it belongs to.

The poller does one thing with what it receives: give it to the user's dialog.
Which side of a process boundary that dialog is on is not its business — so it
depends on this narrow port, and the two implementations are the two ways a
deployment can be arranged:

- **in-process** — the bridge itself, when the poller and the core run
  together (`TelegramBridgeRegistry`);
- **over HTTP** — this module, when ingestion is its own process talking to
  the service through a balancer.

Long polling is why the second one exists at all. One token may be polled by
exactly one process, so the moment there is more than one pod, ingestion has
to live outside them or they steal each other's updates.
"""

import base64
import logging
from typing import Protocol

import httpx
from octoforge_core.domain import Attachment, MessageKind

from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX

logger = logging.getLogger(__name__)

MESSAGES_PATH = "/api/dialog/messages"
USER_ID_HEADER = "X-User-Id"
CHANNEL_HEADER = "X-Channel"
#: What the service names the status behind a 403 (`octoforge_server.deps`).
ACCESS_STATUS_HEADER = "X-Access-Status"


class AccessRefusedError(Exception):
    """The service refused this person: `status` is what it called them.

    Ingestion cannot decide admission — it has no core database and knows
    accounts, not people — so the service decides and this carries the
    verdict back far enough for the surface to say it out loud.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"access refused: {status}")
        self.status = status


class DialogGateway(Protocol):
    """One chat's way into its dialog."""

    async def handle_text(  # noqa: PLR0913, PLR0917 — transport-shaped boundary signature
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
        kind: MessageKind = MessageKind.OWN,
        origin: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        """Submit what the user said."""
        ...


class GatewayRegistry(Protocol):
    """Where the poller gets a chat's gateway."""

    async def gateway_for(self, handle: str, chat_id: int) -> DialogGateway:
        """The gateway for this chat, creating one if needed."""
        ...

    async def person_of(self, external_id: str) -> str:
        """The person behind a Telegram account (statuses and plans key on them).

        An HTTP registry cannot resolve and echoes the handle back — over
        that arrangement the service resolves and gates on its own side.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever was held."""
        ...


class ApiDialogGateway:
    """One chat's dialog, reached over HTTP.

    Sends the account id rather than a person: which person an account belongs
    to is the service's answer, not ingestion's, and asking here would put a
    database this process does not otherwise need behind every message.
    """

    def __init__(self, client: httpx.AsyncClient, external_id: str) -> None:
        self._client = client
        self._external_id = external_id

    async def handle_text(  # noqa: PLR0913, PLR0917 — mirrors the port
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
        kind: MessageKind = MessageKind.OWN,
        origin: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        """Submit the message to whichever pod owns this user's dialog.

        `reply_to_message_id` is dropped on this path: which exchange a sent
        message belonged to is remembered by the pod that sent it, and asking
        across the boundary would cost more than the shortcut saves. Routing
        falls back to the LLM router, which is what it does for any reply it
        cannot resolve.
        """
        await self._post(
            MESSAGES_PATH,
            {
                "content": content,
                "client_message_id": client_message_id,
                "kind": kind.value,
                "origin": origin,
                "attachments": [{"kind": item.kind.value, "ref": item.ref} for item in attachments],
            },
        )

    async def _post(self, path: str, body: dict[str, object] | None) -> None:
        response = await self._client.post(
            path,
            json=body,
            headers={USER_ID_HEADER: self._external_id, CHANNEL_HEADER: TELEGRAM_CHANNEL},
        )
        refusal = response.headers.get(ACCESS_STATUS_HEADER)
        if response.status_code == httpx.codes.FORBIDDEN and refusal:
            raise AccessRefusedError(refusal)
        response.raise_for_status()


class ApiGatewayRegistry:
    """Gateways that reach the service over HTTP, one per chat.

    Stateless beyond the client: there is nothing to keep per chat, because
    rendering happens on the pod. That is the difference between the two
    arrangements in one sentence.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def gateway_for(self, handle: str, chat_id: int) -> DialogGateway:
        return ApiDialogGateway(self._client, handle.removeprefix(USER_ID_PREFIX))

    async def person_of(self, external_id: str) -> str:
        """Echo the handle: resolution (and the status gate) live on the service."""
        return f"{USER_ID_PREFIX}{external_id}"

    async def aclose(self) -> None:
        """The HTTP client is owned by whoever opened it."""


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Basic credentials, rendered once for the whole client."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}
