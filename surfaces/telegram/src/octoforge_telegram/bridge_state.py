"""Shared rendering state and labels for one Telegram bridge."""

import asyncio
from dataclasses import dataclass, field

from octoforge_telegram.drafts import DraftStore

TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
RETRY_LINE_TEMPLATE = "🔁 Провайдер недоступен ({reason}), повтор {attempt} через {delay:.0f} сек"
THINKING_LABEL = "💭 думаю"
TOOL_GROUPS: dict[str, str] = {
    "recall": "🧠 вспоминаю",
    "memory_store": "🧠 запоминаю",
    "memory_delete": "🧠 забываю",
    "history_search": "💬 читаю историю",
    "web_search": "🔎 ищу",
    "http_request": "🌐 запрашиваю",
    "external_call": "🔌 вызываю",
    "endpoint_get": "🔌 вызываю",
    "image_look": "🖼 смотрю",
    "cron_pause": "⏰ планирую",
    "cron_resume": "⏰ планирую",
    "data_put": "📊 записываю",
    "data_query": "📊 выбираю",
    "data_forget": "📊 стираю",
    "instruction_save": "📚 сохраняю",
    "instruction_delete": "📚 удаляю",
    "task_create": "🗂 запускаю",
    "task_delete": "🗂 отменяю",
    "task_list": "🗂 сверяюсь",
    "ask_user": "❓ спрашиваю",
}
STATUS_SEPARATOR = "  "
MAX_PLAIN_DOTS = 3
REPLY_TARGET_MAP_SIZE = 512
TERMINAL_FLUSH_RETRY_DELAY_SECONDS = 1.0


@dataclass(slots=True)
class TelegramBridgeOptions:
    edit_throttle_seconds: float
    drafts: DraftStore | None = None


@dataclass(slots=True)
class StatusEntry:
    label: str
    count: int = 1
    counted: bool = True


@dataclass(slots=True)
class Draft:
    message_id: int | None = None
    buffer: str = ""
    delivered_text: str = ""
    sealed_chunks: int = 0
    reply_to: int | None = None
    pending_since: float | None = None
    flush_timer: asyncio.Task[None] | None = None
    status: list[StatusEntry] = field(default_factory=list)
    exchange_id: str | None = None
