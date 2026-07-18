"""Tests for the Telegram Bot API update models."""

import pytest
from pydantic import ValidationError

from octoforge_web.telegram.models import (
    TelegramChat,
    TelegramChatType,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)

UPDATE_ID = 42
CHAT_ID = 100500
MESSAGE_ID = 7
USER_NAME = "Alice"
MESSAGE_TEXT = "hello"
MESSAGE_DATE = 1784353000


def test_update_parses_with_extra_fields_ignored() -> None:
    payload = {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": MESSAGE_ID,
            "from": {"id": CHAT_ID, "first_name": USER_NAME, "is_bot": False},
            "chat": {"id": CHAT_ID, "type": "private", "first_name": USER_NAME},
            "text": MESSAGE_TEXT,
            "date": MESSAGE_DATE,
        },
    }

    update = TelegramUpdate.model_validate(payload)

    assert update.update_id == UPDATE_ID
    assert update.message is not None
    assert update.message.message_id == MESSAGE_ID
    assert update.message.from_user is not None
    assert update.message.from_user.id == CHAT_ID
    assert update.message.chat.type is TelegramChatType.PRIVATE
    assert update.message.text == MESSAGE_TEXT


def test_update_without_message() -> None:
    update = TelegramUpdate.model_validate({"update_id": UPDATE_ID})

    assert update.message is None


def test_unknown_chat_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TelegramChat.model_validate({"id": CHAT_ID, "type": "mystery"})


def test_message_accepts_populated_from_alias() -> None:
    message = TelegramMessage(
        message_id=MESSAGE_ID,
        from_user=TelegramUser(id=CHAT_ID),
        chat=TelegramChat(id=CHAT_ID, type=TelegramChatType.PRIVATE),
    )

    assert message.from_user is not None
    assert message.text is None
