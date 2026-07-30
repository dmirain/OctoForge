# Инвайты для Telegram-бота + админский тул управления (план, не реализовано)

> Статус: **реализовано** (2026-07-22). Одно уточнение к плану: гейт членства (и
> регистрация `admin_manage`) активируется только при непустом
> `OF_TELEGRAM_ADMIN_IDS` — без админов некому выдавать инвайты, и гейт запер бы
> всех, включая владельца; при пустом списке поверхность работает по-старому,
> открыто. План ниже — исходная спецификация, оставлена как есть.

## Контекст

Сейчас Telegram-бот не авторизует никого: любой, кто написал в личку, автоматически
получает диалог (`poller.py:119-121`). Нужен доступ по приглашениям без поднятия HTTP
— используем то, что уже есть: Telegram deep-link `/start <код>` приходит как обычное
текстовое сообщение через существующий long-poll, HTTP не требуется.

Решение: персональные одноразовые коды в БД, генерируемые и отзываемые через
админский тул, доступный только списку админов из `.env`.

**Принципиальное требование: инвайты — целиком Telegram-специфичная концепция и не
должны затрагивать `core/` вообще** — ни новых доменных модулей, ни новых таблиц в
основной БД, ни новой Alembic-ревизии. Всё хранение, схема и логика инвайтов живут
только в `web/`, в собственной, полностью отдельной SQLite-базе — core об инвайтах
ничего не знает и не может узнать даже случайно (не тот `Base`, не та БД, не та
Alembic-цепочка).

## Архитектура

### 1. Отдельная БД для Telegram-состояния — `web/src/octoforge_web/telegram/invites/`

Своя SQLite-база (`OF_TELEGRAM_DATABASE_URL`, по умолчанию
`sqlite+aiosqlite:///./telegram.db`), полностью независимая от основной
`OF_DATABASE_URL`. Общий с core движок берём только по-честному переиспользуемые
generic-хелперы (не доменную логику): `create_engine`/`create_session_factory` из
`core/src/octoforge_core/db/engine.py:21-29` — это универсальные обёртки над
SQLAlchemy, у core нет ни малейшего знания, для чего именно строится engine.

- **`models.py`** — свой `DeclarativeBase` (НЕ `octoforge_core.db.base.Base` — так
  таблица инвайтов физически не попадёт в `Base.metadata` core и не всплывёт ни в
  одном core-инструменте). `InviteRow`: `id`, `code` (unique, indexed), `status`,
  `note`, `created_at`/`claimed_at`/`revoked_at`, `claimed_by` (indexed, nullable),
  `disabled_cron_job_ids: JSON list[str] | None` — id крон-задач, выключенных именно
  этим отзывом (для точечного `restore_invite`, см. п.3).
  Тип даты — можно переиспользовать `octoforge_core.db.base.UTCDateTime` (это просто
  универсальный `TypeDecorator`, а не доменная сущность, импорт `web → core` и так
  разрешён направлением зависимостей) либо продублировать его в 5 строк локально,
  если хочется нулевой связи с `core.db` — на усмотрение при реализации.
- **`api.py`** — `InviteStatus(StrEnum)`: `PENDING`, `CLAIMED`, `REVOKED`; DTO
  `Invite`; `InviteNotFoundError`, `InviteAlreadyClaimedError`; лёгкий `Protocol
  InviteStore` (ради подменяемости в тестах, как принято в проекте):
  `create(note) -> Invite`, `get_by_code(code) -> Invite | None`,
  `claim(code, user_id) -> Invite` (атомарный CAS — `UPDATE ... WHERE code=... AND
  status='pending'`, `rowcount>0` решает гонку — та же техника, что в
  `cron/store.py:112-135`, просто теперь в web-коде), `get_by_user(user_id) -> Invite
  | None`, `list_all() -> list[Invite]`, `revoke(invite_id) -> Invite`.
- **`store.py`** — `SqlAlchemyInviteStore(session_factory)`, `session_factory`
  привязан к отдельному telegram-engine.
- **Bootstrap схемы** — без Alembic (одна маленькая изолированная таблица, чужая
  Alembic-цепочка ей не нужна): при старте `runtime()`, если Telegram включён,
  `await InviteBase.metadata.create_all(bind=telegram_engine)` — аналог `init_db`
  (`db/engine.py:31-39`), но не через core-функцию, а собственным вызовом на своём
  `Base`/engine, чтобы не тянуть core в этот процесс вообще.

Никакого `tools.py` здесь — исполняемый скилл (п.3) использует `InviteStore` напрямую.

### 2. Гейт членства в `web/src/octoforge_web/telegram/poller.py`

`dispatch()` (`poller.py:104-125`) сейчас матчит `/start` только точным сравнением, а
инвайт-код после пробела улетит в бридж как обычный текст. Меняем:

- Админы (`chat.id`/`from_user.id` из `settings.telegram_admin_ids`) — проходят гейт
  всегда, инвайт не нужен.
- `/start <код>` — пробуем `InviteStore.claim(code, user_id)`; успех → приветствие,
  пропускаем; `InviteAlreadyClaimedError`/не найден → "код недействителен".
- Уже есть активный (не отозванный) инвайт на этот `user_id`
  (`get_by_user(user_id).status == CLAIMED`) → пропускаем как сегодня.
- Иначе (нет инвайта и это не `/start <код>`) → вежливый отказ ("нет доступа, попросите
  инвайт у администратора"), без создания бриджа/раннера.

### 3. Админский тул — `web/src/octoforge_web/telegram/admin.py`

Один консолидированный скилл `AdminManageSkill`, имя `admin_manage`, параметр
`action: "list_users" | "generate_invite" | "revoke_invite" | "restore_invite"` (+
`note` для generate, `user_id`/`invite_id` для revoke/restore) — по формату
`SkillSpec`/`execute()` как в `cron/tools.py:83-100`.

- Проверка допуска — первой строкой `execute()` (см. ниже про видимость на уровне
  списка тулов — это второй, defense-in-depth рубеж): `context.channel ==
  "telegram"` и `context.user_id.removeprefix("tg:")` (см. `poller.py:120`,
  `USER_ID_PREFIX = "tg:"` в `client.py:12`) входит в `settings.telegram_admin_ids` —
  иначе `"error: not authorized"` без побочных эффектов.
- **`list_users`** — соединяет:
  - `InviteStore.list_all()` (кто приглашён/когда/кем отозван, включая `REVOKED`);
  - новый метод `MessageRepository` — агрегат "число сообщений + суммарный размер
    контента" по `user_id`, сгруппированный join `messages` → `dialogs`
    (`func.count`, `func.sum(func.length(...))`, фильтр `channel="telegram"`) — по
    образцу `func.count`/`func.coalesce` уже используемых в `repositories.py:80-84`;
  - `DialogRepository` — время последней активности (`DialogRow.updated_at`, уже
    обновляется на каждый `append`, `repositories.py:99-101`) — понадобится метод
    `list_by_channel(channel)`, возвращающий полные `Dialog`, а не только id (сейчас
    есть только `list_user_ids_by_channel`, `repositories.py:42-48`);
  - `CronStore.list_for_user(user_id)` (`cron/store.py:62-71`) — список крон-задач
    пользователя и их `enabled`-статус.
- **`generate_invite`** — `InviteStore.create(note)`, возвращает код текстом (MVP —
  без deep-link: не заводим зависимость на `getMe`/username бота; админ просто
  пересылает код, приглашённый пишет боту `/start <код>` вручную).
- **`revoke_invite`** (по `user_id`, основной путь — уже присоединившийся; или
  `invite_id` для ещё не использованного кода) — **отзыв обратим**, ничего не
  удаляется:
  1. `InviteStore.revoke(invite_id)` — дальше `poller.py` блокирует новые входящие
     сообщения этого пользователя;
  2. крон-задачи и отложенные (фоновые) задачи — **разные сущности, разная судьба**:
     - крон-задачи (`CronStore.list_for_user(user_id)`) — **выключаются, не
       удаляются**: `CronStore.set_enabled(user_id, job_id, enabled=False)` для
       каждой (метод уже есть, `cron/store.py:80-94`, используется `cron_pause`).
       Список выключенных этим отзывом id сохраняется в самой записи инвайта
       (`InviteRow.disabled_cron_job_ids: JSON`), чтобы `restore_invite` включил
       обратно именно их, а не все паузы пользователя (включая те, что он поставил
       сам через `cron_pause` до отзыва);
     - отложенные (фоновые, `task_spawn`) задачи и вообще любой уже запущенный
       процесс (в т.ч. процесс, который в этот момент как раз выполняет
       сработавшую крон-задачу) — **не трогаем**, дают доработать естественным
       образом. Новых запусков не будет — ни от новых сообщений (гейт в poller),
       ни от крона (выключен), но то, что уже идёт, добивается до конца.
  3. опционально — уведомление через `TelegramClient.send_message` в чат
     пользователя (`chat_id_from_user_id(user_id)`, уже есть в `poller.py:144-151`).
- **`restore_invite`** (по `user_id`/`invite_id`) — обратная операция:
  `InviteStore` переводит инвайт `REVOKED → CLAIMED` (тот же `claimed_by`, доступ
  восстановлен без нового кода), затем `CronStore.set_enabled(..., enabled=True)`
  ровно для id из `disabled_cron_job_ids`, после чего список очищается.

### 4. Регистрация только на Telegram-поверхности

В `web/src/octoforge_web/main.py`, сразу после `registry = build_skill_registry(...)`
(`main.py:135-157`) — `SkillRegistry.register()` (`skills/registry.py:13-18`) мутирует
объект на месте, порядок относительно `build_agent_loop` не важен:

```python
if settings.telegram_bot_token and settings.telegram_admin_ids:
    registry.register(AdminManageSkill(invites, cron_store, messages, dialogs, settings.telegram_admin_ids))
```

`core/composition.py`/`build_skill_registry` не трогаем вообще — веб-чат и сборка без
Telegram этот тул никогда не видят.

### 5. Настройки — `web/src/octoforge_web/config.py`

```python
telegram_admin_ids: list[int] = Field(default_factory=list)
```
Парсинг из `.env` как `OF_TELEGRAM_ADMIN_IDS=123456,789012` — комма-разделённая
строка удобнее JSON-массива для ручного редактирования `.env`
(`external_call_auth_whitelist` — единственный существующий list-settings, это JSON
объектов, для простого списка int сделаем validator, принимающий CSV). Добавить в
`.env.example`.

### 6. Видимость тула только для админов — на уровне списка тулов, не только `execute()`

Сегодня в проекте нет вообще никакой фильтрации тулов по пользователю:
`SkillRegistry.specs()` (`skills/registry.py:27-29`) отдаёт спеки всех
зарегистрированных скиллов без разбора, `AgentLoop` зовёт `self._registry.specs()`
безусловно (`agent/loop.py:246`). Чтобы `admin_manage` не то что не выполнялся, а
вообще не попадал в список тулов, видимый LLM не-админа, добавляем **общий**
(не завязанный на "админа"/Telegram) хук видимости — core по-прежнему ничего не
знает про инвайты или админов, просто даёт скиллам необязательную возможность
самим решать, показывать ли себя в данном контексте:

- `skills/base.py` — `Skill` Protocol не меняем (не заставляем все существующие
  скиллы имплементировать новый метод); видимость — необязательный duck-typed opt-in.
- `skills/registry.py` — `SkillRegistry.specs(context: SkillContext | None = None)
  -> list[SkillSpec]`: для скилла с опциональным атрибутом
  `visible_to(context) -> bool` вызывает его и включает спеку только при `True`;
  скиллы без этого атрибута (подавляющее большинство) видимы всегда.
- `agent/loop.py` — `_run()` считает `specs = self._registry.specs(context)` один
  раз за прогон (`context` там уже есть как параметр) и передаёт `specs` в
  `_stream_assistant(...)` вместо того, чтобы тот сам звал `self._registry.specs()`
  внутри (`loop.py:246`).
- `AdminManageSkill.visible_to(self, context: SkillContext) -> bool` (в `web/`):
  `context.channel == TELEGRAM_CHANNEL and _numeric_id(context.user_id) in
  self._admin_ids`.

Итог: не-админ (и веб-чат, если Telegram вообще выключен — тул туда не
регистрируется, см. п.4) никогда не увидит `admin_manage` в списке тулов; проверка
в `execute()` (п.3) — второй рубеж на случай прямого вызова.

## Про места, которые всё же касаются `core/`

Изоляция инвайтов (хранение, схема) — полная. Но `list_users` должен читать
статистику по существующим core-сущностям, а видимость тула — общая механика
tool-calling. Это не «утечка» концепции инвайтов в core — core как не знал, так и
не будет знать про инвайты/админов; это малые generic-добавки, полезные сами по
себе:

- `core/src/octoforge_core/db/repositories.py` — `DialogRepository.list_by_channel`
  (сейчас есть только `list_user_ids_by_channel`, `repositories.py:42-48`) и
  агрегатный запрос `MessageRepository` "число сообщений + суммарный размер контента
  по `user_id`" (по образцу `repositories.py:80-84`) — read-only, без изменения схемы.
- `core/src/octoforge_core/skills/registry.py` + `agent/loop.py` — опциональный
  контекстно-зависимый хук видимости тула (п.6) — общая механика tool-calling, не
  специфична ни для инвайтов, ни для админов.

Если хочется полностью нулевого диффа в `core/` для статистики — можно вместо
методов репозитория дать админ-скиллу доступ к тому же `session_factory` основной
БД и писать SQL прямо в `web/`, но это разошлось бы с конвенцией проекта
(SQLAlchemy вне `db/`/store-файлов не встречается нигде в core, а тут пришлось бы
завести его в `web/` для чужих таблиц) — предлагаю read-only методы в существующих
репозиториях как меньшее из двух отступлений. Видимость тула (хук в
registry/loop), в отличие от статистики, обойти вообще нечем — это в принципе
может жить только в `core/`, раз тулы собираются и передаются LLM там.

## Файлы, которые меняются/добавляются

- `web/src/octoforge_web/telegram/invites/{api,models,store}.py` — новые, отдельная
  БД (`OF_TELEGRAM_DATABASE_URL`), свой `Base`, без Alembic
- `web/src/octoforge_web/telegram/poller.py` — гейт членства + `/start <код>`
- `web/src/octoforge_web/telegram/admin.py` — новый, `AdminManageSkill`
- `web/src/octoforge_web/config.py` — `telegram_admin_ids`, `telegram_database_url`
- `web/src/octoforge_web/main.py` — второй engine/session_factory для
  `telegram_database_url`, bootstrap схемы инвайтов, постройка `SqlAlchemyInviteStore`,
  условная регистрация `AdminManageSkill`
- `core/src/octoforge_core/db/repositories.py` — два read-only метода (см. выше)
- `core/src/octoforge_core/skills/registry.py` — `specs(context=None)` с опциональной
  фильтрацией по `visible_to`
- `core/src/octoforge_core/agent/loop.py` — специфики считаются один раз в `_run()`
  с `context`, передаются в `_stream_assistant`
- `.env.example`, `docs/design.md` (по конвенции CLAUDE.md — доки обновляются вместе
  с кодом, при реализации)

## Вне рамок этой итерации

- Deep-link с username бота (`t.me/Bot?start=code`) — код передаётся вручную, ссылку
  можно добавить позже, зная `OF_TELEGRAM_BOT_USERNAME` или через `getMe`.
- Гейт для веб-чата — эта задача касается только Telegram; веб-чат не трогаем и не
  меняем в этой итерации.

## Проверка

- `make check` в обоих проектах.
- Новые тесты:
  - CAS-гонка `InviteStore.claim` (два конкурентных claim на один код — только один
    успешен, по образцу гонки в `cron` claim);
  - `poller.dispatch` — админ проходит без инвайта, обладатель `CLAIMED`-инвайта
    проходит, чужой без инвайта получает отказ и не создаёт раннер/диалог, `/start
    <код>` клеймит и пропускает;
  - `SkillRegistry.specs(context)` — скилл с `visible_to` включается/выключается по
    контексту, скиллы без `visible_to` видны всегда (регресс: `specs()` без
    аргумента = все, как раньше);
  - `admin_manage` — `list_users`/`generate_invite`/`revoke_invite`/`restore_invite`;
    `revoke_invite` выключает (не удаляет) крон-задачи и не трогает уже запущенные
    процессы/отложенные задачи; `restore_invite` включает обратно ровно те
    крон-задачи, что выключил соответствующий revoke, не трогая паузы, поставленные
    самим пользователем; не-админ получает `not authorized` при прямом вызове и не
    видит тул в списке вообще.
- Ручная проверка: `make run-telegram`, написать боту от обычного (не админского)
  аккаунта без кода → вежливый отказ; сгенерировать инвайт от админского аккаунта;
  зайти новым аккаунтом с `/start <код>` → доступ; отозвать → дальнейшие сообщения
  отклоняются, крон-задачи выключены (видны в `cron_list`, но не срабатывают),
  запущенная в моменте отзыва задача доигрывается; восстановить → доступ и старые
  крон-задачи снова активны.
