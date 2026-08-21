"""HTTP implementation of the Telegram dialog gateway seam."""

import base64

import httpx

from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX
from octoforge_telegram.gateway_types import (
    AccessRefusedError,
    DialogGateway,
    DialogSubmission,
)

MESSAGES_PATH = "/api/dialog/messages"
USER_ID_HEADER = "X-User-Id"
CHANNEL_HEADER = "X-Channel"
ACCESS_STATUS_HEADER = "X-Access-Status"


class ApiDialogGateway:
    def __init__(self, client: httpx.AsyncClient, external_id: str) -> None:
        self._client = client
        self._external_id = external_id

    async def handle_text(self, submission: DialogSubmission) -> None:
        response = await self._client.post(
            MESSAGES_PATH,
            json={
                "content": submission.content,
                "client_message_id": submission.client_message_id,
                "kind": submission.kind.value,
                "origin": submission.origin,
                "attachments": [
                    {"kind": item.kind.value, "ref": item.ref} for item in submission.attachments
                ],
            },
            headers={USER_ID_HEADER: self._external_id, CHANNEL_HEADER: TELEGRAM_CHANNEL},
        )
        refusal = response.headers.get(ACCESS_STATUS_HEADER)
        if response.status_code == httpx.codes.FORBIDDEN and refusal:
            raise AccessRefusedError(refusal)
        response.raise_for_status()


class ApiGatewayRegistry:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def gateway_for(self, handle: str, chat_id: int) -> DialogGateway:
        return ApiDialogGateway(self._client, handle.removeprefix(USER_ID_PREFIX))

    async def person_of(self, external_id: str) -> str:
        return f"{USER_ID_PREFIX}{external_id}"

    async def aclose(self) -> None:
        return None


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}
