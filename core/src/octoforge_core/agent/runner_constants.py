"""Policy constants for dialog actors."""

SUBSCRIBER_QUEUE_SIZE = 100
STREAM_CLOSED = None
CLAIM_HEARTBEAT_SECONDS = 5.0
CLAIM_STALE_AFTER_SECONDS = 30.0

SPAWN_REFUSAL_TEMPLATE = (
    "cannot spawn: process limit ({limit}) reached; active: {titles} — ask the user what to cancel"
)
PROCESS_LIMIT_NOTICE_TEMPLATE = (
    "I could not start handling '{message}': the process limit ({limit}) is reached. "
    "Active processes: {titles}. Cancel one of them or wait for it to finish."
)
CRON_LIMIT_NOTICE_TEMPLATE = (
    "Cron job '{title}' could not start: the process limit ({limit}) is reached. "
    "Active processes: {titles}."
)
TARIFF_LIMIT_NOTICE_TEMPLATE = (
    "I cannot answer right now: the daily limit of your plan is exhausted "
    "({reason}: {used}/{limit}). Your message is saved; the counter resets at midnight UTC."
)
TARIFF_CRON_LIMIT_NOTICE_TEMPLATE = (
    "Scheduled run '{title}' was skipped: the daily limit of your plan is exhausted "
    "({reason}: {used}/{limit}). It will fire again after midnight UTC."
)
TARIFF_SPAWN_REFUSAL_TEMPLATE = (
    "cannot start a background task: the daily limit of the user's plan is exhausted "
    "({reason}: {used}/{limit}) — tell the user and stop"
)
SPAWNED_TEMPLATE = "task {task_id} spawned"
ANSWER_NOTE_KEY = "answer"
SUBMIT_FAILED_ERROR = "your message could not be saved — please send it again"
RESTART_LIMIT_ERROR = "could not resume after the service restart: process limit reached"
TARIFF_RESTART_ERROR_TEMPLATE = (
    "could not resume after the service restart: the daily limit of the plan "
    "is exhausted ({reason}: {used}/{limit})"
)
DEFAULT_TASK_ERROR = "unknown error"
MAX_LOOK_IMAGES = 6
BACKGROUND_TASK_PROMPT = (
    "You are solving a background task. User message is the task. "
    "Produce the final answer as the result."
)
DATE_ENVELOPE_TEMPLATE = "[Current date and time: {now} (UTC)]\n{content}"
CURRENT_DATE_FORMAT = "%Y-%m-%d %H:%M"
NUDGE_TEMPLATE = (
    "Кстати, я всё ещё жду ответа по «{title}» — я спрашивал: «{question}». "
    "Ответь, когда будет удобно, или скажи, что это уже неактуально."
)
NUDGE_AFTER_SECONDS = 300.0
MATERIAL_QUIET_SECONDS = 30.0
MATERIAL_TITLE_TEMPLATE = "Переслано от {origin}"
MATERIAL_TITLE_ANONYMOUS = "Пересланные сообщения"
MATERIAL_TITLE_IMAGES = "Присланные изображения"
MATERIAL_DIGEST_MESSAGES = 20
MATERIAL_DIGEST_CHARS = 4000
MATERIAL_DIGEST_ELLIPSIS = "\n[…]\n"
MATERIAL_DIGEST_TEMPLATE = (
    "The user forwarded {count} message(s) — third-party content, not their "
    "own words. Which exchange does this material belong to?\n{lines}"
)
