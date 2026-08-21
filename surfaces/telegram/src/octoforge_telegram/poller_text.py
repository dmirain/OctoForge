"""User-visible Telegram commands, notices and narrative markers."""

COMMAND_START = "/start"
COMMAND_SECRETS = "/secrets"
COMMAND_INVITE = "/invite"
MAX_MENU_DESCRIPTION_CHARS = 24
SECRETS_MENU_DESCRIPTION = "Пароли и токены"
INVITE_MENU_DESCRIPTION = "Пригласить друга"
REFERRAL_PREFIX = "ref_"
GREETING_TEXT = (
    "Привет! Я OctoForge - напиши вопрос, и я постараюсь помочь. "
    "Чтобы прервать ответ, так и напиши: «стой»."
)
WELCOME_TEXT = (
    "Код принят, добро пожаловать! Я OctoForge - напиши вопрос, и я постараюсь помочь. "
    "Чтобы прервать ответ, так и напиши: «стой»."
)
ACCESS_DENIED_TEXT = (
    f"Нет доступа: обратитесь к администратору за инвайт-кодом и отправьте {COMMAND_START} <код>."
)
INVITE_INVALID_TEXT = (
    "Этот код недействителен или уже использован. Обратитесь к администратору за новым кодом."
)
TEXT_ONLY_NOTICE = "Пока понимаю только текстовые сообщения."
MATERIAL_ATTRIBUTION_TEMPLATE = "[переслано от {origin}]"
MATERIAL_ATTRIBUTION_ANONYMOUS = "[переслано]"
MATERIAL_PLACEHOLDER = "(вложение без текста)"
VOICE_MIN_SECONDS = 1
SECONDS_PER_MINUTE = 60
VOICE_TOO_SHORT_NOTICE = "Запись слишком короткая - я ничего не услышал. Скажи ещё раз?"
VOICE_TOO_LONG_TEMPLATE = (
    "Эта запись длиннее {limit} минут - столько я за раз не расшифрую. "
    "Пришли фрагмент короче или напиши текстом."
)
VOICE_EMPTY_NOTICE = "Я не разобрал, что сказано в записи. Напиши текстом или запиши ещё раз?"
VOICE_PLAN_NOTICE = "Распознавание голосовых не входит в твой тариф - напиши текстом."
VISION_PLAN_NOTICE = "Распознавание изображений не входит в твой тариф - опиши словами."
QUEUE_WAIT_TEXT = (
    "Сейчас все места заняты - я записал тебя в очередь на доступ. "
    "Напишу, как только место освободится."
)
ACCESS_CLOSED_TEXT = "Доступ закрыт."
REFERRAL_LINK_TEXT = (
    "Твоя персональная ссылка-приглашение:\n{link}\n\n"
    "Отправь её другу - по ней он попадёт в бота. Если все места заняты, он встанет в очередь."
)
REFERRAL_CODE_TEXT = (
    "Твой персональный код-приглашение: {code}\n"
    "Для активации друг отправляет боту команду "
    + COMMAND_START
    + " {code}. "
    + "Если все места заняты, он встанет в очередь."
)
REFERRALS_DISABLED_TEXT = "Приглашения не настроены на этой инсталляции."
SECRETS_LINK_TEXT = (
    "Ссылка на форму секретов (действует 10 минут):\n{url}\n\n"
    "Значения шифруются, ассистент видит только коды секретов. "
    "Никогда не присылайте секреты сообщением в чат."
)
SECRETS_DISABLED_TEXT = "Хранилище секретов не настроено на этой инсталляции."
GROUP_NOTICE = "Пока работаю только в личных чатах."
