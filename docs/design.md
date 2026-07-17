# OctoForge — мультипользовательский LLM-агент (аналог openclaw, знания в БД)

> Дизайн-документ. Живой: обновляется по мере развития идеи, до и во время реализации.

## Концепция

Агент на Python/FastAPI/SQLAlchemy. Отличия от openclaw:

- **Знания и скилы — в БД** (SQLite через async SQLAlchemy), а не в файлах.
- **Скилы — исполняемые Jinja-шаблоны (DSL)**: рендер шаблона = выполнение. Скилы общие для всех пользователей.
- **Мультипользовательность**: токен на пользователя, знания изолированы по `user_id`, скилы — общие.
- **Фоновые задачи**: агент может увести работу в фон и вернуться с результатом.

## Инженерные практики

Конвенции кода зафиксированы в `AGENTS.md` — это источник истины для всех, кто пишет код проекта. Суть:

- все даты — timezone-aware UTC, время только через хелпер `utc_now()` (`octoforge_core/time.py`);
- полная типизация (ruff `ANN` + mypy strict), объекты вместо словарей, перечисления через `StrEnum`, без магических строк и чисел;
- тесты в том же изменении, что и код;
- чистая архитектура: библиотека `core/` (домен + логика, чистый Python) и приложение `web/` (FastAPI) — физически разные проекты со своими зависимостями и тестами;
- DI: зависимости передаём снаружи (конструктор, параметры, `Depends`), объекты внутри методов не создаём;
- проверки в порядке «линтер → типы → тесты», одна команда `make check`;
- контроль сложности: `C901` ≤ 10, `PLR0915` ≤ 50 statements, `PLR0911` ≤ 6 return'ов — превышение решаем дроблением, а не noqa;
- языки: документация и общение — на русском, коммиты и docstrings — на английском;
- любое изменение логики или новая идея дописывается в эту документацию в том же изменении;
- коммиты — только с явного разрешения пользователя.

## Структура репозитория

Монорепо из двух независимых Python-проектов (src-layout), у каждого свой `pyproject.toml`,
свои зависимости и свои тесты. Web-приложение зависит от библиотеки.

```
Makefile, README.md, .env.example
core/                          # библиотека octoforge-core — домен и логика; fastapi запрещён
  pyproject.toml               # deps: httpx, sqlalchemy[asyncio], aiosqlite; dev: pytest, pytest-asyncio, ruff, mypy
  src/octoforge_core/
    domain.py                  # ChatMessage, ToolCall, MessageRole, Dialog
    config.py                  # LLMConfig
    time.py                    # utc_now() — единая точка времени (UTC aware)
    errors.py                  # LLMResponseError
    ports.py                   # Protocol-порты: LLMClient, TaskStore
    agent/
      events.py                # LoopEvent: IterationStarted, TextDelta, AssistantMessage,
                               #   ToolCallRequested/Completed/Failed, Finished, Cancelled, Failed
      control.py               # LoopControl — mailbox инъекций + флаг отмены
      loop.py                  # AgentLoop.stream(history, control, context) → AsyncIterator[LoopEvent]
      prompts.py               # DEFAULT_SYSTEM_PROMPT (answer-first, task_spawn, уведомления)
      runner.py                # ConversationRunner (актор), ConversationManager, ConversationEvent
    skills/
      base.py                  # Skill (Protocol), SkillSpec, SkillOrigin (BASIC|DYNAMIC), SkillContext
      registry.py, errors.py   # реестр + ошибки
      basic/                   # http_request.py, task_spawn.py, task_list.py
    tasks/
      models.py                # Task, TaskKind (SKILL|PROMPT), TaskStatus (PENDING|RUNNING|DONE|FAILED)
      store.py                 # InMemoryTaskStore (реализация порта TaskStore, для тестов)
      runner.py                # TaskRunner — фоновое выполнение задач
      errors.py                # TaskNotFoundError
    db/
      base.py                  # Declarative Base + UTCDateTime (aware UTC на чтении/записи)
      models.py                # ORM-модели: DialogRow, MessageRow, TaskRow
      engine.py                # create_engine/create_session_factory (DI) + init_db (create_all)
      repositories.py          # DialogRepository, MessageRepository, SqlAlchemyTaskStore
      errors.py                # DialogNotFoundError
    llm/
      events.py                # StreamEvent: TextDelta | StreamFinished
      openai.py                # OpenAI-совместимый клиент: complete() + stream() (SSE, tools)
    # дальше: skills/dynamic/ (Jinja-движок), память
  tests/                       # test_agent_loop, test_conversation_runner, test_db_repositories,
                               # test_http_request_skill, test_openai_client, test_openai_stream,
                               # test_skills_registry, test_tasks
web/                           # приложение octoforge-web — FastAPI-обёртка
  pyproject.toml               # deps: octoforge-core, fastapi, uvicorn, pydantic-settings
  src/octoforge_web/
    main.py                    # app factory + composition root (DI: БД, LLM, реестр, акторы,
                               #   TaskRunner; канал "web")
    config.py                  # Settings (env с префиксом OF_, включая OF_DATABASE_URL)
    deps.py                    # провайдеры зависимостей из app.state + заголовок X-User-Id
    api/
      dialog.py                # messages/cancel/events(SSE) по (user_id, channel)
      sse.py                   # сериализация LoopEvent → SSE-кадры
      schemas.py               # pydantic-схемы запросов/ответов
    static/index.html          # чат-UI: SSE-стрим, шаги скилов, кнопка «Стоп», поле имени (= user_id)
  tests/                       # test_dialog_api.py, test_sse.py
```

## Петля агента: события, управление, актор

### Петля как поток событий (`octoforge_core/agent/loop.py`)

`AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` — не «возвращает ответ»,
а выдаёт поток событий (`agent/events.py`):

- `IterationStarted(index)` — начало итерации рассуждения;
- `TextDelta(text)` — токен ответа по мере стриминга LLM;
- `AssistantMessage(message, interrupted)` — завершённое сообщение итерации (interrupted — обрыв по отмене);
- `ToolCallRequested / ToolCallCompleted(output) / ToolCallFailed(error)` — шаги скилов;
- `Finished(message)` — финальный ответ; `Cancelled` — отмена; `Failed(error)` — срыв без ответа.

`history` мутируется на месте (в неё дописываются новые сообщения) — владелец списка (актор)
забирает накопленное в историю диалога.

### Управление прогоном (`agent/control.py`)

`LoopControl`: mailbox (`asyncio.Queue`) для инъекций + флаг отмены. Точки обработки:

- перед каждым вызовом LLM — дрейн mailbox в историю (инъекции пользователя и уведомления задач);
- на каждый чанк стрима — проверка отмены: стрим обрывается (`aclose`), частичный текст
  сохраняется как `AssistantMessage(interrupted=True)` и остаётся в истории, эмитится `Cancelled`.

Инъекция никогда не вклинивается между assistant(tool_calls) и его tool-результатами —
только на безопасных границах итераций.

### Актор диалога (`agent/runner.py`)

`ConversationRunner` — актор на диалог: единый inbox команд (`_Submit`, `_Cancel`, `_TaskDone`)
сериализует всё, что происходит с диалогом. Хранит историю (in-memory, при создании пересобранную
из БД), владеет текущим `LoopControl`, ведёт подписчиков (очереди для SSE-broadcast, seq
нумерация событий).

- диалог = (user_id, channel): `ConversationManager.get_or_create_runner(user_id, channel)` —
  get-or-create строки `dialogs` через `DialogRepository` (явного создания диалога нет, он
  существует по факту обращения — см. [dialogs.md](dialogs.md)); история пересобирается из
  `messages` (включая tool-сообщения и прерванные ответы) — диалог переживает перезапуск.
- сообщения персистятся: пользовательское — синхронно при submit (до старта прогона);
  assistant/tool — по мере дописывания в рабочую историю во время прогона (актор следит за
  «хвостом» истории и пишет каждое новое сообщение со следующим `seq`); прерванные частичные
  ответы тоже сохраняются.
- submit в idle → новый прогон; submit во время прогона → инъекция в `LoopControl` (руление).
- `notify_task_done` в idle → system-сообщение с результатом + прогон (проактивное сообщение
  пользователю); во время прогона → инъекция. Передав уведомление актору, менеджер помечает
  задачу `result_delivered=True` (`TaskStore.mark_delivered`).
- инъекции не теряются: сообщения, не дочитанные прогоном к его концу (mailbox), переставляются
  обратно в inbox актора и получают собственный прогон — уведомление о задаче, пришедшее на
  последней итерации, всё равно будет озвучено пользователю.
- `ConversationManager` — реестр runner'ов по dialog_id (создание под lock'ом), точка входа
  для `TaskRunner.on_task_done`. Канал для ядра — непрозрачная строка; конкретные значения
  (`"web"`, будущий `"telegram"`) объявляют адаптеры в composition root.

### Системный промпт (`agent/prompts.py`)

`DEFAULT_SYSTEM_PROMPT`: отвечать «сначала суть, потом детали» (прерывание полезно),
«в фоне» → `task_spawn` и продолжить диалог, при system-уведомлении о задаче — коротко
сообщить результат, для HTTP — `http_request`, статусы задач — `task_list`.

### Стриминг LLM (`llm/openai.py`)

`complete()` — обычный (non-streaming) вызов, используется фоновыми задачами.
`stream()` — `stream=true`: парсинг SSE-чанков (delta.content → `TextDelta`,
delta.tool_calls — аккумуляция по index со склейкой arguments), в конце `StreamFinished(message)`.
`aclose()` на генераторе обрывает HTTP-соединение — основа отмены.

## Фоновые задачи

- `tasks/models.py`: `Task` (id, dialog_id, user_id, channel, title, kind, input, status,
  result, error, result_delivered, created_at/started_at/finished_at — через `utc_now()`);
  `TaskKind(SKILL|PROMPT)`, `TaskStatus(PENDING|RUNNING|DONE|FAILED)`. `channel` и `user_id`
  денормализованы в задачу: уведомление уходит на поверхность, с которой задача запущена,
  без join'а к dialogs.
- `TaskStore` (Protocol в `ports.py`): add/get/list(dialog_id)/next_pending/mark_running/
  mark_done/mark_failed/mark_delivered. Реализации: `SqlAlchemyTaskStore`
  (`db/repositories.py`, боевой путь) и `InMemoryTaskStore` (тесты); актор и раннер от
  реализации не зависят.
- `tasks/runner.py`: `TaskRunner` — asyncio-цикл: берёт PENDING, выполняет (PROMPT → `llm.complete`;
  SKILL → скил из реестра), фиксирует DONE/FAILED, вызывает колбэк `on_task_done(task)`
  (в web — `ConversationManager.notify_task_done`). Старт/стоп — в lifespan.
- результат считается доставленным, когда менеджер передал его актору диалога
  (`result_delivered=True`). Редоставка результатов, оставшихся недоставленными после
  перезапуска (диалог ещё не поднят в менеджере), **не реализована** — отдельная итерация.

Скилы задач: `task_spawn` (title + prompt → PROMPT-задача текущего диалога) и
`task_list` (задачи диалога со статусами и результатами).

## Скилы

> **Модель уточнена**: скилы стали одним из типов инструкций в общем хранилище (знание/скил/тул)
> с векторным поиском — см. [instructions.md](instructions.md). Типизация BASIC/DYNAMIC отменена;
> раздел ниже описывает состояние на момент реализации.

Скил — единица, которую агент вызывает через LLM tool calling. Два типа:

- **Базовые (`SkillOrigin.BASIC`)** — код проекта (`octoforge_core/skills/basic/`): `http_request`,
  `task_spawn`, `task_list`. Подключаются в composition root.
- **Динамические (`SkillOrigin.DYNAMIC`)** — Jinja-шаблоны из БД (появятся с БД). Реестр един:
  `SkillRegistry` хранит скилы обоих типов под уникальными именами.

Абстракция (`skills/base.py`): `SkillSpec` (name, description, parameters_schema — JSON Schema);
`Skill` (Protocol): `spec` + `async execute(arguments, context) -> str`;
`SkillContext` — per-invocation контекст (user_id, channel, dialog_id; позже память).
Аргументы валидирует сам скил (`SkillArgumentsError`).

### Динамические скилы (план, после появления БД)

`skills/dynamic/engine.py`: `SandboxedEnvironment(enable_async=True, undefined=StrictUndefined)`,
рендер через `render_async`. В шаблоне: `params`, `http`, `memory` (скоп юзера + глобальные),
`llm.ask` (one-shot, без вложенных скилов). Пример:

```jinja
{% set r = http.get("https://api.example.com/users/" ~ params.user_id) %}
{% if r.status == 200 %}
  {% set summary = llm.ask("Summarize: " ~ r.text) %}
  {{ memory.store("user_" ~ params.user_id, summary, tags=["api"]) }}
  {{ summary }}
{% else %}
  API error: {{ r.status }}
{% endif %}
```

### Будущие базовые скилы (план)

- `memory.store / memory.search / memory.delete` — скоп `user` или `global` (enum MemoryScope)
- `skill.list / skill.run / skill.save / skill.delete` — каталог и запись динамических скилов

## Модель данных

ORM-модели — в `octoforge_core/db/models.py`; доменные объекты — в `octoforge_core/domain.py`
и `octoforge_core/tasks/models.py`; маппинг ORM↔домен — в репозиториях
(`octoforge_core/db/repositories.py`). Таблицы `users` пока **нет**: `user_id` — доверенная
непрозрачная строка от клиента, аутентификация отложена (см. [plan.md](plan.md)). Схема:

- **dialogs**: `id` (uuid str PK), `user_id` (str, index), `channel` (str), `created_at`,
  `updated_at`; unique (`user_id`, `channel`)
- **messages**: `id`, `dialog_id` FK (index), `seq` (int, монотонно растёт в рамках диалога;
  unique (`dialog_id`, `seq`)), `role` (значение MessageRole), `content`, `tool_calls`
  (JSON, nullable), `tool_call_id` (nullable), `created_at`
- **tasks**: `id`, `dialog_id` FK (index), `user_id` (index), `channel`, `kind`, `title`,
  `input` (JSON), `status`, `result`, `error`, `result_delivered` (bool, default False),
  `created_at`, `started_at`, `finished_at`

Все `*_at` — timezone-aware UTC: `UTCDateTime` (`db/base.py`) принудительно выставляет UTC при
чтении/записи (SQLite возвращает naive datetime). Создание схемы — `init_db` (`create_all`) в
lifespan; Alembic появится при первой деструктивной миграции.

План следующих этапов: **memories** (`user_id` nullable = global, `key`, `content`, `tags` JSON,
даты; unique (`user_id`, `key`)) и **instructions** (знание/скил/тул + embedding) — см.
[instructions.md](instructions.md), [data-store.md](data-store.md).

## API (`octoforge_web/api/`)

Реализовано (диалог — get-or-create по (user_id, channel); канал `"web"` объявлен в composition root):

- все эндпоинты диалога требуют заголовок `X-User-Id` (доверенная строка до появления
  аутентификации); отсутствующий/пустой → 400
- `POST /api/dialog/messages` `{content}` → 202 `{status: "accepted"}` — сообщение; во время
  прогона становится инъекцией
- `POST /api/dialog/cancel` → 202 — мягкая отмена текущего прогона
- `GET /api/dialog/events` — SSE-подписка на события диалога (`iteration_started`,
  `text_delta`, `assistant_message`, `tool_call_*`, `finished`, `cancelled`, `failed`;
  heartbeat-комментарии; в кадрах `seq` и `dialog_id`); диалог создаётся при первом обращении,
  поэтому подписаться можно до первого сообщения
- `GET /health`, `GET /` — чат-UI (SSE-стрим токенов, шаги скилов, «Стоп», поле имени =
  user_id, уходит заголовком `X-User-Id`; EventSource не умеет кастомные заголовки, поэтому
  стрим читается через fetch)

План:

- аутентификация: `users`, токены (`POST /api/users` с admin-secret, `Authorization: Bearer`)
- `GET /api/skills`, `GET /api/tasks`
- `GET /ws` — при необходимости двунаправленного канала

## Тесты (pytest + pytest-asyncio)

Реализовано:

- `core/tests/test_openai_stream.py` — SSE-парсинг (дельты, склейка tool_calls, [DONE]), `aclose`
- `core/tests/test_openai_client.py` — non-streaming вызов, tools, tool-история, ошибки
- `core/tests/test_agent_loop.py` — события прогона, инъекция mid-run, отмена с частичным текстом, ошибка скила, лимит итераций
- `core/tests/test_conversation_runner.py` — submit → события, inject во время прогона, cancel,
  task_done → проактивное сообщение + `result_delivered`, персист сообщений по ходу прогона,
  пересборка истории после «перезапуска» менеджера (SQLite :memory:)
- `core/tests/test_db_repositories.py` — диалоги (get-or-create, уникальность пары), сообщения
  (seq/порядок, tool_calls round-trip, изоляция), UTCDateTime round-trip, SqlAlchemyTaskStore
  (те же сценарии, что у InMemoryTaskStore, + mark_delivered)
- `core/tests/test_tasks.py` — spawn/валидация, runner DONE/FAILED + колбэк, task_list по диалогу,
  mark_delivered, UTC-даты
- `core/tests/test_http_request_skill.py`, `core/tests/test_skills_registry.py`
- `web/tests/test_dialog_api.py` — get-or-create диалога, изоляция двух user_id, 400 без
  `X-User-Id`, messages/cancel/events (SSE через генератор), health, UI
- `web/tests/test_sse.py` — сериализация событий в SSE-кадры

План:

- `test_skills_engine.py`, `test_memory.py`, auth-тесты

## Порядок работ

> Детальная дорожная карта по этапам A–F (БД → инструкции → датасеты → память →
> роутер → крон), включая решение отложить аутентификацию (user_id — доверенная
> строка от клиента), — в [plan.md](plan.md).

1. ✅ Скаффолд монорепо: библиотека `core/` + приложение `web/`, Makefile, ruff/mypy/pytest
2. ✅ Чат без аутентификации: UI, `POST /api/chat`, ядро — прокси к LLM
3. ✅ Петля + базовые скилы: `AgentLoop` (tool calling), `Skill/SkillSpec/SkillRegistry`, `http_request`
4. ✅ Событийная петля + актор диалога + фоновые задачи (in-memory): стриминг токенов (SSE), инъекции, отмена, `task_spawn`/`task_list`, проактивные уведомления, системный промпт answer-first
5. ✅ (без аутентификации: user_id — доверенная строка от клиента; users/токены отложены) БД (SQLAlchemy async, SQLite), перенос историй/задач в БД; диалоги keyed by (user, channel), поверхности — см. [dialogs.md](dialogs.md); две инсталляции (standalone/distributed) — см. [scaling.md](scaling.md)
6. Динамические скилы: Jinja-движок + `skill.save/run`; скилы памяти (user/global)
7. Агентный контекст: память в контексте, `GET /api/skills`, `GET /api/tasks`
8. LLM-роутер и процессная модель диалога — решения согласованы, см. [process-model.md](process-model.md); реализация отложена
9. Инструкции в БД (знание/скил/тул + векторный поиск) — см. [instructions.md](instructions.md); крон-задачи — см. [cron.md](cron.md); датасеты пользовательских данных — см. [data-store.md](data-store.md); реализация после БД
10. Бэклог из обзора openclaw (SSRF-гвард, формула поиска, каталог скилов, детали крона и пр.) — см. [openclaw-review.md](openclaw-review.md)

## Проверка

- `make check` (ruff → mypy → pytest) — всё зелёное
- Ручной сценарий: `make run` → http://127.0.0.1:8000 → токены текут по мере генерации; «выполни GET к <url>» — шаг скилла виден в чате; «реши в фоне X» — агент подтверждает и продолжает диалог, результат приходит сам; «Стоп» — ответ обрывается; два разных имени — истории и задачи изолированы; перезапуск приложения не теряет диалог (история восстанавливается из БД)
- Целевой сценарий: два юзера → память не смешивается, скил общий, задачи разных юзеров изолированы
