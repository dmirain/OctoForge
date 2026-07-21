# План реализации (дорожная карта этапов A–G) и бэклог доработок

> Утверждённый план по документам `docs/*` + сводный бэклог доработок (замечания
> пользователя + самоотчёт агента, проверено по коду 2026-07-20 — вердикты ниже).
> Живой: статусы обновляются по мере выполнения.
> Источники: [design.md](design.md) (шаги 5–10), [dialogs.md](dialogs.md),
> [instructions.md](instructions.md), [data-store.md](data-store.md),
> [process-model.md](process-model.md), [cron.md](cron.md), [scaling.md](scaling.md),
> [openclaw-review.md](openclaw-review.md).

## Рамка

- Аутентификацию **не делаем**: она будет на уровне клиента. Ядро и API принимают
  `user_id` (непрозрачная строка, доверенная) и работают с ним. Таблица `users`,
  токены, admin-secret — отложены (см. «Дальше»).
- Порядок этапов A→F: каждый следующий опирается на предыдущий; между этапами — ревью.
- Каждый этап = код + тесты + обновление документации в одном изменении;
  `make check` зелёный в конце каждого этапа.

## Этап A. БД + диалоги (user_id, channel) + персист сообщений и задач ✅

(design.md шаг 5 без аутентификации, scaling.md этап 1, dialogs.md)

1. Зависимости: core += `sqlalchemy[asyncio]`, `aiosqlite`; web Settings +=
   `OF_DATABASE_URL` (default `sqlite+aiosqlite:///./octoforge.db`).
2. `core/src/octoforge_core/db/`: `base.py` (Base + `UTCDateTime` TypeDecorator),
   `models.py` (`dialogs`, `messages`, `tasks`; `user_id` — строковая колонка с
   индексом, без таблицы `users`), `engine.py` (async engine/session, `create_all`
   при старте; Alembic — при первой деструктивной миграции), `repositories.py`
   (DialogRepository, MessageRepository, `SqlAlchemyTaskStore`; `InMemoryTaskStore`
   остаётся для тестов).
3. Ядро: `ConversationManager` ключуется по `(user_id, channel)`; runner пересобирает
   историю из `messages`; сообщения персистятся; `SkillContext` += `user_id`, `channel`;
   `Task.conversation_id` → `dialog_id`.
4. Web API: `user_id` заголовком `X-User-Id` (400, если пустой); диалог — get-or-create:
   `POST /api/dialog/messages`, `POST /api/dialog/cancel`, `GET /api/dialog/events`;
   `/api/conversations/*` удалить.
5. UI: поле имени = `user_id` (localStorage), уходит заголовком `X-User-Id`.
6. Тесты: репозитории на SQLite `:memory:`, SqlAlchemyTaskStore, пересборка истории
   после «перезапуска»; web — get-or-create, изоляция двух `user_id`, 400 без заголовка.
7. Доки: design.md (шаг 5 → ✅, «Модель данных»/«API» — как реализовано), AGENTS.md
   (core зависит от sqlalchemy; fastapi по-прежнему запрещён).

**Проверка**: `make check`; ручной сценарий — два имени в UI, истории/задачи изолированы,
перезапуск приложения не теряет диалог.

## Этап B. Инструкции в БД + векторный поиск + external_call + SSRF-гвард ✅

(instructions.md, openclaw-review.md бэклог п.1)

**Ключевое требование — обособленный модуль.** Всё, что связано с инструкциями
(хранение, поиск, ранжирование), — самодостаточный пакет `core/instructions/`,
который легко выделить в отдельный сервис или заменить целиком. Граница модуля —
единый фасад `InstructionService` (Protocol); петля и скилы видят только его, не зная
про SQL, эмбеддинги и ранжирование. **Исполнение — на стороне core**: модуль только
хранит, ищет и ранжирует; внешние вызовы выполняет core-исполнитель поверх записей,
полученных через фасад. DTO фасада — JSON-совместимые объекты (под будущую
HTTP-границу); таблицы модуля — его собственность, вынос в сервис не тянет чужие таблицы.

1. Пакет `core/src/octoforge_core/instructions/`:
   - `api.py` — фасад `InstructionService` (Protocol) + DTO (`Instruction`,
     `InstructionType`, `SearchHit`): `search(query, k)`, `save(...)`,
     `get_by_name(name)`;
   - `local.py` + `store.py` + `models.py` + `ranking.py` — локальная реализация:
     таблица `instructions` (`knowledge|skill|tool`: id, type, title, content, embedding
     JSON, tags JSON, version, usage_count, success_count, даты), SQL-стор, cosine
     brute-force (standalone-объёмы), буст точного `title`. Полная формула openclaw
     (70/30 + MMR + затухание) — отдельной итерацией, подменой `ranking`;
   - будущая `http.py` — реализация фасада клиентом выделенного сервиса (в этап не
     входит; интерфейс сразу проектируем под неё). Выбор реализации — composition root.
2. Исполнитель внешних вызовов — **вне модуля**, в core (`net/external.py`): читает
   tool-запись через `get_by_name`, рендерит url-шаблон и параметры по схеме из записи,
   подставляет служебную авторизацию только для белого списка base-url (конфиг
   composition root), выполняет HTTP.
3. Порт `EmbeddingClient` + OpenAI-совместимый клиент `llm/embeddings.py`; модуль
   получает его конструктором (DI). Конфиг рядом с `LLMConfig`; web Settings +=
   `OF_EMBEDDING_*`. Конфиг модуля (top-k и пр.) — в Settings, пробрасывается
   конструктором, не читается из env напрямую.
4. Рантайм-тулы `instructions_search`, `instruction_save` — тонкие скилы-адаптеры над
   фасадом; `external_call` — скил над исполнителем из п.2 (фасад нужен ему только для
   чтения tool-записи).
5. SSRF-гвард (`core/net/guard.py`): resolve хоста → `ipaddress`-проверки
   (private/loopback/link-local/reserved, 169.254.169.254) → отказ; применяется в
   `http_request` и в исполнителе `external_call`.
6. Сидирование при пустой таблице: generic http tool + 1–2 скила-примера (seed-модуль
   внутри пакета, вызов в lifespan).
7. Системный промпт: правила `instructions_search`/`external_call`, когда сохранять
   новые инструкции.
8. Тесты: embeddings client (mock), локальная реализация фасада на `:memory:`
   (search/save/ранжирование/сид), исполнитель external_call (шаблоны, whitelist, блок
   SSRF с мокнутым resolver). Контрактные тесты фасада оформить так, чтобы они же могли
   проверять будущую http-реализацию.

## Этап C. Датасеты пользовательских данных ✅

(data-store.md)

1. Таблицы `datasets` (owner_user_id, name, description, schema JSON, usage_notes,
   retention, embedding, version, даты) и `dataset_records` (dataset_id FK cascade,
   owner_user_id, payload JSON, created_at).
2. Порт `DatasetStore` + SQL-реализация; валидация записи по `schema` — исполнителем.
3. Тулы `data_put` (create-if-absent), `data_query` (равенство полей, диапазон дат,
   limit), `data_forget` (каскадное удаление).
4. Дескрипторы участвуют в `instructions_search`.
5. Owner-изоляция в исполнителе по `SkillContext.user_id` (проверяет тул, не LLM).
6. Тесты: создание/валидация/фильтры, изоляция юзеров, каскадное удаление.

## Этап D. Память (per-user, кросс-поверхностная) ✅

(design.md «Модель данных», dialogs.md «факт модели»)

1. Таблица `memories` (id, user_id nullable = global, key, content, tags JSON, даты;
   unique(user_id, key)) + порт `MemoryStore` + SQL-реализация.
2. Скилы `memory_store` / `memory_search` / `memory_delete` (имена с подчёркиваниями:
   точки несовместимы с OpenAI tool-calling), `MemoryScope(USER|GLOBAL)`.
3. Промпт: когда класть/читать память; автоинъекция в контекст — отдельной итерацией.
4. Тесты: скоупы, изоляция, unique-замещение по ключу.

## Этап E. Процессная модель + LLM-роутер ✅

(process-model.md; крупный рефактор runner'а)

1. `agent/router.py`: `RouteAction` (INJECT|START_NEW|CANCEL|PROMOTE), `RouteOp`,
   `RouteDecision`, порт `MessageRouter`, `LLMRouter` (one-shot complete + tool
   `route(ops)`, валидация, таймаут, фолбэк).
2. Актор → менеджер процессов: narrative + `Process` (fg/bg), один форграунд, broadcast
   только форграунда + маркеры; пакет операций применяется по порядку.
3. `TaskStore` += `cancel`/`is_cancelled`/`count_active`; `TaskStatus` += `CANCELLED`;
   `TaskKind` → `RUN`; глобальный `TaskRunner` упраздняется, фон — pump-процессы актора.
4. `SkillContext` += порт `TaskSpawner` (лимит `OF_MAX_PROCESSES` → текст-отказ);
   детерминированный guardrail лимита в акторе.
5. События `ProcessSuspended/Resumed/Completed` → SSE + маркеры в UI.
6. Гигиена истории (openclaw-review п.4): пометка прерванного тёрна; уведомление о
   завершении фонового с защитой от двойной отправки.
7. Тесты: роутер (mock LLM: пакеты, фолбэк, passthrough), процессы
   (switch/promote/cancel/лимит/уведомления), регрессии runner'а.

## Этап F. Крон-задачи ✅

(cron.md + openclaw-review п.5–6)

1. Таблица `cron_jobs` (user_id, channel, title, schedule, timezone IANA, prompt,
   enabled, next_fire_at, last_fire_at, claimed_by, created_at) + порт `CronStore`.
2. Планировщик (asyncio-цикл, зависимость `croniter`, `zoneinfo`): выборка due,
   CAS-захват (`claimed_by` + lease TTL), coalesce пропущенных, пересчёт `next_fire_at`;
   догонялка с разбросом и лимитом реплея.
3. HTTP API (скоуп по `X-User-Id`; служебные токены — позже, с аутентификацией):
   `POST/GET/DELETE /api/cron/jobs`, `POST /api/cron/jobs/{id}/pause|resume`.
4. Сид tool-записи «cron API» в instructions → агент создаёт задачи через
   `external_call` (без выделенных скилов).
5. Выстрел: `POST /api/dialogs/{key}/wake` → фоновый процесс (Этап E) с `prompt`,
   процесс помечен id крон-задачи → уведомление; переполнение лимита → отложенное
   уведомление о невозможности запуска.
6. Тесты: расписание/таймзоны, claim CAS, coalesce, wake.

## Этап G. Telegram-адаптер ✅

(dialogs.md, design.md «Telegram-адаптер»)

1. Пакет `web/src/octoforge_web/telegram/` (core о транспорте не знает): `models.py`
   (pydantic, `extra="ignore"`, алиас `from`, `TelegramChatType(StrEnum)`), `client.py`
   (порт `TelegramClient` + `TelegramBotClient` на httpx — без aiogram), `bridge.py`
   (рендер событий runner'а в черновик с throttle-правками, статус-строки скилов,
   чанкер 4096), `poller.py` (long-poll, offset в памяти, backlog-drain, backoff) +
   `TelegramBridgeRegistry`.
2. Поверхность: канал `"telegram"`, `user_id = "tg:<telegram user id>"`, только личные
   чаты; команды `/start` и `/cancel`; идентичности web/telegram не связываются.
3. Зависимость моста — `RunnerProvider` (callable → runner); в composition root —
   `ConversationManager.get_or_create_runner`; подписка на события ДО submit.
4. Прогрев при старте: мосты для диалогов канала telegram из БД
   (`DialogRepository.list_user_ids_by_channel`) — крон-выстрелы и уведомления
   доставляются после рестарта.
5. Конфиг: `OF_TELEGRAM_BOT_TOKEN` (пусто = выключен), `OF_TELEGRAM_POLL_TIMEOUT_SECONDS`,
   `OF_TELEGRAM_EDIT_THROTTLE_SECONDS`.
6. Тесты: модели, мост (дельты → правки, статус-строки, чанкинг, отмена/ошибка),
   поллер (команды, offset, drain, восстановление, прогрев).

## Дальше (за рамками плана)

Настоящая аутентификация (`users`, токены, служебные токены для wake/cron);
distributed-профиль (scaling.md этап 2); честный подсчёт токенов контекста
(компакция истории сделана — [context.md](context.md));
секреты-заглушки; `GET /api/skills`, `GET /api/tasks`; полная формула поиска
(70/30 + MMR); подтверждение новых скилов человеком; раннее исполнение тулов
(eager tool execution) — дизайн в [streaming.md](streaming.md); живучесть задач
(редоставка недоставленных результатов при старте, реанимация/фейл осиротевших
процессов после рестарта); автоинъекция памяти в контекст; outbox для поверхностей.

## Бэклог доработок

> Сводный список того, что надо доработать: замечания пользователя + самоотчёт агента.
> Каждый пункт самоотчёта проверен по коду (2026-07-20) — вердикт в таблице ниже.
> То, что уже отслеживается в «Дальше» выше и в [quality-audit.md](quality-audit.md),
> здесь не дублируется, а даётся ссылкой.

### Вердикты по самоотчёту агента (проверено по коду)

| Пункт агента | Вердикт | Факт по коду |
|---|---|---|
| Идемпотентность вызовов (дубли одного tool, как с напоминаниями про черешню) | **Закрыто на уровне скилла** (2026-07-20) | `cron_create` дедупит по `(title, schedule, prompt, one_shot)` → «already exists» (`core/.../skills/basic/cron_jobs.py`). Общего dedup tool-вызовов в петле по-прежнему нет — остаётся в разделе 1. |
| Обработка ошибок + ретрай крон-прогонов | **Закрыто** (2026-07-20) | Исход процесса возвращается в стор: `TaskOutcomeListener` (актор) → `CronOutcomeReporter` → `record_fire_result`; `last_status`/`last_error` в `cron_jobs`, ретрай с backoff'ом (`OF_CRON_RETRY_LIMIT`/`OF_CRON_RETRY_BACKOFF_SECONDS`). |
| Логирование и мониторинг | **Частично** | Stdlib-логирование есть (7 файлов, `basicConfig` в `web/.../main.py:201-204`). Нет JSON-логов, request-id/корреляции, метрик, трейсинга — уже отражено в [quality-audit.md](quality-audit.md) (P0-наблюдаемость, остаток на P1). |
| Rate limiting | **Подтверждено** | Лимитеров нет нигде. Есть только потолки: `agent_max_iterations=10`, `max_processes=5` на диалог, таймаут роутера, cap SSE-очереди. Уже отражено в [quality-audit.md](quality-audit.md) (S3, волна 4). |
| Стриминг ответов | **Опровергнуто — уже есть** | Web: SSE `text_delta`, UI дописывает дельты инкрементально (`web/.../api/sse.py:57-58`). Telegram: черновое сообщение с throttle-правками (`web/.../telegram/bridge.py:126-128,152-156`, `OF_TELEGRAM_EDIT_THROTTLE_SECONDS=1.5`). В бэклог не берём. |

### 1. Агент и промпты

- ~~**Путаница крон vs фоновая задача**~~ — **исправлено** (2026-07-20): правила 3/11
  системного промпта разводят `task_spawn` (работа сейчас, результат один раз) и
  `cron_create` (расписание и напоминания, включая разовые с `one_shot=true`);
  граница продублирована в описаниях скилов `task_spawn`/`cron_create` и в
  сид-сценарии `schedule_a_recurring_report`.
- **Идемпотентность tool-вызовов (dedup).** ~~Для `cron_create`~~ — **сделано**
  (дедуп по `(title, schedule, prompt, one_shot)` → «already exists»). Остаётся общий
  вариант: dedup-ключ в петле (хэш tool+args в рамках прогона, повтор → вернуть
  результат первого вызова).
- ~~**Одноразовые напоминания**~~ — **сделано** (2026-07-20): `one_shot` в `cron_jobs`
  и `cron_create`, агент строит датированное cron-выражение, задача удаляется после
  первого успешного прогона (см. [cron.md](cron.md)).

### 2. Крон

- ~~**Ретрай и наблюдаемость фейлов выстрела**~~ — **исправлено** (2026-07-20): исход
  процесса репортится актором через порт `TaskOutcomeListener` → `CronOutcomeReporter`
  → `record_fire_result`; `last_status`/`last_error`/`retry_count` в `cron_jobs`
  (видны в `cron_list` и HTTP API), ретрай с экспоненциальным backoff'ом
  (`OF_CRON_RETRY_LIMIT`=3, `OF_CRON_RETRY_BACKOFF_SECONDS`=60), CANCELLED без ретрая.
  Подробности — [cron.md](cron.md).

### 3. Telegram

- **Rich Messages.** Bot API 10.1 добавил `sendRichMessage` (нативный markdown, таблицы,
  заголовки, collapsible-блоки, чеклисты). Сейчас клиент умеет только
  `sendMessage`/`editMessageText` с HTML-разметкой (`web/.../telegram/client.py:29-48`,
  `markdown.py`); `sendPhoto`/`sendDocument`/клавиатур нет. Доработка: расширить порт
  `TelegramClient` и мост; учесть конфликт со стримингом — промежуточные `editMessageText`
  ломают rich-разметку (по опыту других проектов; вероятно, финальную версию отправлять
  rich, черновик — как сейчас).
- **Приём файлов/фото.** Сейчас нетекстовые сообщения отклоняются заглушкой «Пока понимаю
  только текстовые сообщения» (`web/.../telegram/poller.py:113-115`; в моделях нет полей
  photo/document — `telegram/models.py:35-43`). Доработка: модели под photo/document/voice,
  `getFile` + скачивание, подача в контекст (vision/текстовое описание), лимиты размера.

### 4. UX и интеграции

- **Автоопределение языка ответа.** В системном промпте нет указания отвечать на языке
  пользователя — вероятно, достаточно одной строки в промпт; проверить на практике.
- **Интеграция с календарями** (Google/Apple Calendar) для напоминаний и планирования.
  Зависит от аутентификации/OAuth (см. раздел 5).
- **Дашборд** — веб-интерфейс просмотра данных: диалоги, задачи, cron-задачи, память,
  датасеты. Частично упирается в отсутствующие GET-эндпоинты (`GET /api/skills`,
  `GET /api/tasks` — уже в «Дальше» выше).
- ~~Стриминг ответов~~ — уже реализован на обеих поверхностях (см. таблицу вердиктов).

### 5. Безопасность и эксплуатация

- **Аутентификация** — уже в «Дальше» выше и в [quality-audit.md](quality-audit.md)
  (S1, волна 4): users, токены, служебные токены для wake/cron. Дополнительно: allowlist
  telegram user id — сейчас бот отвечает любому аккаунту, который его нашёл
  (`web/.../telegram/poller.py:119-125`).
- **Хранилище секретов.** Токены уже в `.env` (файл `OctoForgeBotToken.txt` из корня
  убран); дальше — keychain/sops/age. Частично
  пересекается с [quality-audit.md](quality-audit.md) (P1 — операбельность).
- **Версионирование инструкций.** Откат скиллов/инструкций, если новая версия сломалась:
  версии записей в таблице `instructions`, активная версия, откат = переключение активной.
- **Наблюдаемость P1** (структурные JSON-логи, request-id, метрики) и **rate limiting** —
  уже отслеживаются в [quality-audit.md](quality-audit.md) (остаток P0-наблюдаемости, S3,
  волны 2–4), здесь не дублируем.
- ~~**Компакция контекста**~~ — **сделано** (2026-07-20): ветка процесса = системный
  промпт + блок саммари тем + горячий хвост; фоновая LLM-суммаризация при переполнении
  хвоста (`OF_CONTEXT_HOT_MAX_CHARS`), поиск по архиву — скилом `history_search`.
  Подробности — [context.md](context.md). Честный подсчёт токенов и оглавление тем —
  отдельными итерациями (см. «Не входит» там же).

### Предлагаемый порядок

1. ~~Промпт крон/фон + dedup `cron_create`~~ — ✅ сделано (2026-07-20).
2. ~~Компакция контекста / окно нарратива~~ — ✅ сделано (2026-07-20): модуль
   `context/` (саммари в `dialog_summaries` + горячий хвост), скил `history_search`,
   см. [context.md](context.md).
3. Telegram: rich messages, приём файлов/фото.
4. Наблюдаемость P1 (+ ~~ретрай крон-выстрелов~~ — ✅ сделано 2026-07-20).
5. Аутентификация (с allowlist telegram).
6. Календари, дашборд, версионирование инструкций.

## Принятые решения по умолчанию

- ORM и репозитории — в `core/db/`; core получает зависимость sqlalchemy (fastapi —
  по-прежнему запрещён).
- `user_id` — доверенная строка от клиента (`X-User-Id`); изоляция логическая, без
  криптографии — до настоящей аутентификации.
- `create_all` вместо Alembic до первой деструктивной миграции.
- Outbox откладывается до второй поверхности (в standalone уведомления in-process).
- Порядок B→C→D→E→F: инструкции/датасеты/память раньше роутера (в docs «отложен»),
  крон после процессной модели (выстрел = фоновый процесс).
