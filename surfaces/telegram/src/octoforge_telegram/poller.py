"""Public Telegram poller, membership and bridge-registry facade."""

from octoforge_telegram.bridge_registry import (
    BridgeRegistryServices,
    TelegramBridgeRegistry,
)
from octoforge_telegram.membership import (
    MembershipDecision,
    MembershipOptions,
    TelegramMembership,
    chat_id_from_user_id,
)
from octoforge_telegram.poller_format import (
    CAPTION_LABEL,
    IMAGE_FAILED_PLACEHOLDER,
    IMAGE_TAG,
    IMAGE_TAG_NUMBERED,
    VOICE_TAG,
)
from octoforge_telegram.poller_loop import TelegramPoller
from octoforge_telegram.poller_text import (
    ACCESS_CLOSED_TEXT,
    ACCESS_DENIED_TEXT,
    COMMAND_INVITE,
    GREETING_TEXT,
    GROUP_NOTICE,
    INVITE_INVALID_TEXT,
    INVITE_MENU_DESCRIPTION,
    MATERIAL_ATTRIBUTION_ANONYMOUS,
    MATERIAL_PLACEHOLDER,
    MAX_MENU_DESCRIPTION_CHARS,
    QUEUE_WAIT_TEXT,
    REFERRAL_PREFIX,
    REFERRALS_DISABLED_TEXT,
    SECRETS_DISABLED_TEXT,
    SECRETS_MENU_DESCRIPTION,
    TEXT_ONLY_NOTICE,
    VISION_PLAN_NOTICE,
    VOICE_EMPTY_NOTICE,
    VOICE_PLAN_NOTICE,
    VOICE_TOO_SHORT_NOTICE,
    WELCOME_TEXT,
)
from octoforge_telegram.poller_types import (
    DEFAULT_VOICE_MAX_SECONDS,
    TelegramPollerOptions,
)

COMMAND_START = "/start"

__all__ = [
    "ACCESS_CLOSED_TEXT",
    "ACCESS_DENIED_TEXT",
    "CAPTION_LABEL",
    "COMMAND_INVITE",
    "COMMAND_START",
    "DEFAULT_VOICE_MAX_SECONDS",
    "GREETING_TEXT",
    "GROUP_NOTICE",
    "IMAGE_FAILED_PLACEHOLDER",
    "IMAGE_TAG",
    "IMAGE_TAG_NUMBERED",
    "INVITE_INVALID_TEXT",
    "INVITE_MENU_DESCRIPTION",
    "MATERIAL_ATTRIBUTION_ANONYMOUS",
    "MATERIAL_PLACEHOLDER",
    "MAX_MENU_DESCRIPTION_CHARS",
    "QUEUE_WAIT_TEXT",
    "REFERRALS_DISABLED_TEXT",
    "REFERRAL_PREFIX",
    "SECRETS_DISABLED_TEXT",
    "SECRETS_MENU_DESCRIPTION",
    "TEXT_ONLY_NOTICE",
    "VISION_PLAN_NOTICE",
    "VOICE_EMPTY_NOTICE",
    "VOICE_PLAN_NOTICE",
    "VOICE_TAG",
    "VOICE_TOO_SHORT_NOTICE",
    "WELCOME_TEXT",
    "BridgeRegistryServices",
    "MembershipDecision",
    "MembershipOptions",
    "TelegramBridgeRegistry",
    "TelegramMembership",
    "TelegramPoller",
    "TelegramPollerOptions",
    "chat_id_from_user_id",
]
