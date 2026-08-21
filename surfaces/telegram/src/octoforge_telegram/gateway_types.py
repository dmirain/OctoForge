"""Ports and typed message submission at the Telegram/core seam."""

from dataclasses import dataclass
from typing import Protocol

from octoforge_core.agent.runner import DialogSubmission as CoreDialogSubmission
from octoforge_core.domain import Attachment, MessageKind, MessageSource


class AccessRefusedError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"access refused: {status}")
        self.status = status


@dataclass(frozen=True, slots=True)
class DialogSubmission:
    content: str
    client_message_id: str | None = None
    reply_to_message_id: int | None = None
    kind: MessageKind = MessageKind.OWN
    origin: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def to_core(self, reply_to_exchange_id: str | None) -> CoreDialogSubmission:
        """Translate transport metadata into the core submission contract."""
        return CoreDialogSubmission(
            self.content,
            client_message_id=self.client_message_id,
            reply_to_exchange_id=reply_to_exchange_id,
            source=MessageSource(self.kind, self.origin, self.attachments),
        )


class DialogGateway(Protocol):
    async def handle_text(self, submission: DialogSubmission) -> None: ...


class GatewayRegistry(Protocol):
    async def gateway_for(self, handle: str, chat_id: int) -> DialogGateway: ...

    async def person_of(self, external_id: str) -> str: ...

    async def aclose(self) -> None: ...
