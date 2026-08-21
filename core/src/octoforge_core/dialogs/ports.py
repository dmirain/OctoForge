"""Repository ports of the dialogs module."""

from datetime import datetime
from typing import Protocol

from octoforge_core.dialogs.requests import ExchangeSettlement, MessageAppend
from octoforge_core.dialogs.types import (
    DialogClaim,
    DialogClaimList,
    Exchange,
    ExchangeList,
    ExchangeStatus,
    MessageStatsList,
    UserActivityList,
)
from octoforge_core.domain import ChatMessage, Dialog


class ExchangeRepository(Protocol):
    async def create(
        self,
        dialog_id: str,
        title: str,
        status: ExchangeStatus | None = None,
    ) -> Exchange: ...

    async def get(self, exchange_id: str) -> Exchange: ...

    async def list_live(self, dialog_id: str) -> ExchangeList: ...

    async def find_collecting(self, dialog_id: str) -> Exchange | None: ...

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList: ...

    async def touch(self, exchange_id: str) -> None: ...

    async def set_title(self, exchange_id: str, title: str) -> None: ...

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList: ...

    async def list_stranded_dialog_ids(self) -> list[str]: ...

    async def reopen_and_list_stranded(self, dialog_id: str) -> tuple[int, ExchangeList]: ...

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        pending_question: str | None = None,
    ) -> None: ...

    async def settle_owned(self, request: ExchangeSettlement) -> Exchange | None: ...

    async def delete_for_dialog(self, dialog_id: str) -> None: ...


class ClaimRepository(Protocol):
    async def claim(self, dialog_id: str, owner: str) -> DialogClaim: ...

    async def heartbeat(self, claims: DialogClaimList) -> frozenset[str]: ...

    async def release(self, dialog_id: str, owner: str, generation: int) -> None: ...

    async def held_elsewhere(
        self,
        dialog_ids: frozenset[str],
        owner: str,
        stale_before: datetime,
    ) -> frozenset[str]: ...

    async def current_generation(self, dialog_id: str) -> int | None: ...

    async def delete_for_dialog(self, dialog_id: str) -> None: ...


class DialogRepository(Protocol):
    async def get_or_create(self, user_id: str, channel: str) -> Dialog: ...

    async def get(self, dialog_id: str) -> Dialog: ...

    async def list_by_channel(self, channel: str) -> list[Dialog]: ...

    async def delete(self, dialog_id: str) -> None: ...


class MessageRepository(Protocol):
    async def append(self, request: MessageAppend) -> str: ...

    async def append_pair(
        self,
        dialog_id: str,
        first: ChatMessage,
        second: ChatMessage,
    ) -> None: ...

    async def find_by_client_id(self, dialog_id: str, client_message_id: str) -> bool: ...

    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]: ...

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]: ...

    async def last_activity_by_channel(self, channel: str) -> dict[str, datetime]: ...

    async def list(self, dialog_id: str) -> list[ChatMessage]: ...

    async def set_exchange(self, message_id: str, exchange_id: str) -> None: ...

    async def stats_by_channel(self, channel: str) -> MessageStatsList: ...

    async def user_activity_by_channel(self, channel: str, since: datetime) -> UserActivityList: ...
