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


def test_forward_origin_variants_yield_display_names() -> None:
    known_user = TelegramMessage.model_validate(
        {
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
            "forward_origin": {
                "type": "user",
                "date": 1,
                "sender_user": {"id": 7, "first_name": "Иван", "last_name": "Петров"},
            },
        }
    )
    hidden = TelegramMessage.model_validate(
        {
            "message_id": 2,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
            "forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Аноним"},
        }
    )
    channel = TelegramMessage.model_validate(
        {
            "message_id": 3,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
            "forward_origin": {
                "type": "channel",
                "date": 1,
                "chat": {"id": -100, "title": "Ъ"},
                "message_id": 9,
            },
        }
    )
    group = TelegramMessage.model_validate(
        {
            "message_id": 4,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
            "forward_origin": {
                "type": "chat",
                "date": 1,
                "sender_chat": {"id": -200, "title": "Соседи"},
            },
        }
    )

    assert known_user.forward_origin is not None
    assert known_user.forward_origin.display_name == "Иван Петров"
    assert hidden.forward_origin is not None
    assert hidden.forward_origin.display_name == "Аноним"
    assert channel.forward_origin is not None
    assert channel.forward_origin.display_name == "Ъ"
    assert group.forward_origin is not None
    assert group.forward_origin.display_name == "Соседи"


def test_unknown_forward_variant_parses_without_a_name() -> None:
    """A future MessageOrigin variant must not make the whole update unparseable."""
    message = TelegramMessage.model_validate(
        {
            "message_id": 5,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
            "forward_origin": {"type": "something_new", "date": 1, "unknown_field": 42},
        }
    )

    assert message.forward_origin is not None
    assert message.forward_origin.display_name == ""


def test_caption_is_the_body_of_a_media_message() -> None:
    photo = TelegramMessage.model_validate(
        {
            "message_id": 6,
            "chat": {"id": 1, "type": "private"},
            "caption": "смотри",
            "media_group_id": "album-1",
        }
    )
    bare = TelegramMessage.model_validate({"message_id": 7, "chat": {"id": 1, "type": "private"}})

    assert photo.body == "смотри"
    assert photo.media_group_id == "album-1"
    assert bare.body is None
