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
    config.py                  # LLMConfig, EmbeddingConfig
    time.py                    # utc_now() — единая точка времени (UTC aware)
    errors.py                  # LLMResponseError
    ports.py                   # Protocol-порты: LLMClient, TaskStore
    agent/
      events.py                # LoopEvent: IterationStarted, TextDelta, AssistantMessage,
                               #   ToolCallRequested/Completed/Failed, Finished, Cancelled, Failed
      control.py               # LoopControl — mailbox инъекций + флаг отмены
      loop.py                  # AgentLoop.stream(history, control, context) → AsyncIterator[LoopEvent]
      prompts.py               # DEFAULT_SYSTEM_PROMPT (answer-first, task_spawn, уведомления,
                               #   instructions_search / external_call / instruction_save,
                               #   data_put / data_query / data_forget,
                               #   memory_store / memory_search / memory_delete)
      runner.py                # ConversationRunner (актор), ConversationManager, ConversationEvent
    skills/
      base.py                  # Skill (Protocol), SkillSpec, SkillOrigin (BASIC|DYNAMIC), SkillContext
      registry.py, errors.py   # реестр + ошибки
      basic/                   # http_request.py, task_spawn.py, task_list.py,
                               # instructions_search.py, instruction_save.py, external_call.py,
                               # data_put.py, data_query.py, data_forget.py,
                               # memory_store.py, memory_search.py, memory_delete.py
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
      embeddings.py            # EmbeddingClient (Protocol-порт) + OpenAI-совместимый клиент
                               #   (POST /embeddings); порт общий для instructions/ и datasets/
    instructions/              # обособленный модуль инструкций (только хранение/поиск/ранг)
      api.py                   # граница модуля: InstructionService (Protocol), Instruction,
                               #   InstructionType, SearchHit, InstructionNotFoundError
      models.py                # InstructionRow — таблица instructions, собственность модуля
      store.py                 # SQL-стор (сессии через async_sessionmaker, DI)
      ranking.py               # чистые функции: cosine + буст точного title
      local.py                 # LocalInstructionService — локальная реализация фасада
      seed.py                  # SEED_INSTRUCTIONS + seed_if_empty (generic http tool + скилы-примеры)
    datasets/                  # обособленный модуль датасетов (per-user трекеры, этап C)
      api.py                   # граница модуля: DatasetService (Protocol), Dataset, DatasetRecord,
                               #   DatasetSchema/FieldType, DatasetHit, ошибки модуля
      models.py                # DatasetRow + DatasetRecordRow — таблицы datasets/dataset_records
      validation.py            # parse_schema/dump_schema/validate_record (схема и записи)
      store.py                 # SQL-стор (явный каскад удаления, MAX_SCAN_ROWS)
      ranking.py               # свои чистые функции: cosine + буст точного имени (независимость)
      service.py               # LocalDatasetService — локальная реализация фасада
    memory/                    # обособленный модуль памяти (per-user + global, этап D)
      api.py                   # граница модуля: MemoryStore (Protocol-порт), Memory,
                               #   MemoryScope (USER|GLOBAL), MemoryNotFoundError
      models.py                # MemoryRow — таблица memories, собственность модуля
      store.py                 # SqlAlchemyMemoryStore (upsert по (owner, key), LIKE-поиск)
    net/                       # исполнение внешних вызовов (core-сторона, вне модуля)
      guard.py                 # SsrfGuard: resolve хоста (resolver инъектируется) → ipaddress-проверки
      tool_spec.py             # ToolSpec + parse_tool_spec (JSON-формат tool-записи)
      external.py              # ExternalCallExecutor: шаблоны, whitelist-авторизация, SSRF-гвард
      errors.py                # SsrfBlockedError, ToolSpecError, ExternalCallError
    # дальше: skills/dynamic/ (Jinja-движок)
  tests/                       # test_agent_loop, test_conversation_runner, test_data_skills,
                               # test_datasets, test_dataset_validation, test_db_repositories,
                               # test_embeddings, test_external_call, test_http_request_skill,
                               # test_instruction_skills, test_instructions_local,
                               # test_memory, test_memory_skills,
                               # test_openai_client, test_openai_stream, test_skills_registry,
                               # test_ssrf_guard, test_tasks
web/                           # приложение octoforge-web — FastAPI-обёртка
  pyproject.toml               # deps: octoforge-core, fastapi, uvicorn, pydantic-settings
  src/octoforge_web/
    main.py                    # app factory + composition root (DI: БД, LLM, эмбеддер, реестр,
                               #   исполнитель external_call, акторы, TaskRunner; канал "web")
    config.py                  # Settings (env с префиксом OF_, включая OF_DATABASE_URL,
                               #   OF_EMBEDDING_*, OF_INSTRUCTIONS_TOP_K,
                               #   OF_EXTERNAL_CALL_AUTH_WHITELIST, OF_DATASETS_QUERY_*,
                               #   OF_MEMORY_SEARCH_*)
    deps.py                    # провайдеры зависимостей из app.state + заголовок X-User-Id
    api/
      dialog.py                # messages/cancel/events(SSE) по (user_id, channel)
      sse.py                   # сериализация LoopEvent → SSE-кадры
      schemas.py               # pydantic-схемы запросов/ответов
    static/index.html          # чат-UI: SSE-стрим, шаги скилов, кнопка «Стоп», поле имени (= user_id)
  tests/                       # test_dialog_api.py, test_sse.py, test_config.py
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
сообщить результат, для HTTP — `http_request`, статусы задач — `task_list`; перед
нетривиальной задачей — `instructions_search` (знания/сценарии/тулы/датасеты), вызов найденных
тулов — через `external_call`, после нового многошагового сценария — сохранить его
через `instruction_save`; трекинг структурированных данных (еда, вес, привычки) —
датасеты: искать через `instructions_search`, писать через `data_put` (создавая датасет
со схемой при отсутствии), читать/строить отчёты через `data_query`, удалять через `data_forget`.

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
  `task_spawn`, `task_list`, `instructions_search`, `instruction_save`, `external_call`,
  `data_put`, `data_query`, `data_forget`, `memory_store`, `memory_search`, `memory_delete`.
  Подключаются в composition root. Имена скилов — с подчёркиваниями, не с точками:
  точки в function-name несовместимы с OpenAI tool-calling (зафиксированное решение).
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

- `skill.list / skill.run / skill.save / skill.delete` — каталог и запись динамических скилов

## Инструкции и внешние вызовы (этап B)

Модель типов (знание/скил/тул, модульность, безопасность) — в [instructions.md](instructions.md).
Реализация:

- **Модуль `instructions/`** — самодостаточный: хранит, ищет и ранжирует записи трёх типов.
  Граница — `api.py` (Protocol `InstructionService`: `search`/`save`/`get_by_name`, DTO
  JSON-совместимые под будущую HTTP-границу). Локальная реализация `LocalInstructionService`:
  таблица `instructions` (собственность модуля), эмбеддинг `title + "\n" + content` через порт
  `EmbeddingClient` (OpenAI-совместимый клиент `llm/embeddings.py`), ранжирование — brute-force
  cosine + буст точного `title` (`ranking.py`, чистые функции; полная формула 70/30 + MMR —
  позже подменой модуля). `search` инкрементирует `usage_count` возвращённых хитов.
  Сидирование `seed_if_empty` (generic weather tool + два скила-примера) — в lifespan;
  запускается только при настроенном `OF_EMBEDDING_API_KEY` (без ключа приложение стартует
  без сидов — эмбеддинги остаются опциональными до первого вызова search/save).
- **Исполнение — вне модуля** (`net/`): `ExternalCallExecutor` читает tool-запись через
  `get_by_name`, парсит `ToolSpec` (JSON: method, url_template, params_schema, auth),
  валидирует параметры по схеме (required присутствуют, неизвестные запрещены), рендерит
  url-шаблон подстановкой с `urllib.parse.quote`, проверяет URL гвардом, подставляет
  служебную авторизацию только для белого списка base-url-префиксов (конфиг composition
  root, `OF_EXTERNAL_CALL_AUTH_WHITELIST`) и выполняет запрос с `follow_redirects=False`
  (редирект обошёл бы проверку гварда). Тело ответа режется до 8000 символов.
- **SSRF-гвард** (`net/guard.py`): resolve хоста (resolver инъектируется, в тестах — stub)
  → отказ, если ЛЮБОЙ из адресов private/loopback/link-local (включая 169.254.169.254)/
  multicast/reserved/unspecified или не globally-routable (CGNAT 100.64/10). Применён в
  `ExternalCallExecutor` и в скиле `http_request` (там тоже `follow_redirects=False` —
  поведение не изменилось, httpx и раньше не следовал редиректам по умолчанию). Известное
  ограничение TOCTOU/DNS-rebinding задокументировано в docstring гварда.
- **Рантайм-скилы** — тонкие адаптеры: `instructions_search(query, k?)` (k по умолчанию —
  `OF_INSTRUCTIONS_TOP_K`), `instruction_save(type, title, content, tags?)`,
  `external_call(name, params?)`.

## Датасеты пользовательских данных (этап C)

Модель и обоснование — в [data-store.md](data-store.md). Реализация:

- **Модуль `datasets/`** — обособленный, зеркалит `instructions/`: граница `api.py`
  (Protocol `DatasetService`: `create_dataset`/`get_dataset`/`add_record`/`query_records`/
  `delete_dataset`/`search`, DTO JSON-совместимые под будущую HTTP-границу). Локальная
  реализация `LocalDatasetService`: таблицы `datasets` + `dataset_records` (собственность
  модуля), эмбеддинг дескриптора `name + "\n" + description + "\n" + usage_notes` через
  общий порт `EmbeddingClient` (`llm/embeddings.py`, перенесён туда из `instructions/`),
  ранжирование — brute-force cosine + буст точного имени (свой `ranking.py`, от
  instructions не зависит).
- **Схема датасета** — JSON `{"fields": [{"name", "type", "required?"}]}`; типы
  `string|integer|number|boolean|date|datetime` (`validation.py`: bool не считается
  integer/number, date/datetime — ISO-строки; лишние поля записи разрешены). Валидация
  записей — на стороне скилов (`data_put`), сервис доверяет вызывающему.
- **Запросы записей**: SQL-фильтр по `created_at` + `ORDER BY created_at DESC` + cap
  `MAX_SCAN_ROWS` (1000); фильтр `equals` — в памяти, тип-чувствительный (`5 != "5"`);
  агрегация — LLM по выборке. Owner-изоляция — `WHERE owner_user_id` на уровне SQL
  во всех операциях: чужой датасет неотличим от несуществующего.
- **Скилы** — тонкие адаптеры: `data_put(dataset, record, description?, schema?,
  usage_notes?, retention?)` (create-if-absent: при отсутствии датасета schema+description
  обязательны; гонка create-after-get ловится `DatasetExistsError` → повторный get),
  `data_query(dataset, equals?, date_from?, date_to?, limit?)` (date-only = весь день
  UTC; лимиты `OF_DATASETS_QUERY_DEFAULT_LIMIT`/`OF_DATASETS_QUERY_MAX_LIMIT`),
  `data_forget(dataset)` (явный каскад DELETE записей, ответ — счётчик).
- **Дескрипторы в `instructions_search`**: скилу передан `DatasetService` (опциональный
  параметр конструктора), хиты обоих фасадов сливаются по убыванию score; датасеты
  форматируются как `[dataset] <name>` со сниппетом description + списком полей.

## Память (этап D)

Модель (per-user, кросс-поверхностная) — в [dialogs.md](dialogs.md). Реализация:

- **Модуль `memory/`** — обособленный, зеркалит `instructions/` и `datasets/`: граница
  `api.py` (Protocol-порт `MemoryStore`: `put`/`get`/`search`/`delete`, DTO `Memory`
  JSON-совместимый под будущую HTTP-границу, `MemoryScope(USER|GLOBAL)`). Локальная
  реализация `SqlAlchemyMemoryStore`: таблица `memories` (собственность модуля).
  Эмбеддингов нет — поиск ключевой.
- **Скоупы**: `user_id` NULL = глобальная память (общие факты для всех); иначе запись
  принадлежит пользователю и видна ему на всех поверхностях. `put` — upsert по
  (owner, key): существующая запись замещается (id сохраняется, updated_at бампается),
  возвращает `(Memory, created)`. Ограничение unique(user_id, key) НЕ защищает глобальные
  ключи (NULL'ы различны в unique и в SQLite, и в Postgres), поэтому уникальность
  обеспечивает сам стор: select по owner (NULL — через `is_(None)`) → update или insert.
- **Поиск**: `search(user_id, query, limit)` — видимость «свои + глобальные», подстрока
  case-insensitive по key ИЛИ content (SQL ILIKE с экранированием `%`/`_` — запрос остаётся
  литеральной подстрокой), порядок `updated_at DESC` + key для детерминизма, пустой query
  — пустой список без запроса в БД. `get`/`delete` — строго по (owner, key): глобальная
  запись через user-скоуп не читается и не удаляется (`MemoryNotFoundError`).
- **Скилы** — тонкие адаптеры: `memory_store(key, content, tags?, scope?)`,
  `memory_search(query, limit?)` (лимиты `OF_MEMORY_SEARCH_DEFAULT_LIMIT`/
  `OF_MEMORY_SEARCH_MAX_LIMIT`, сниппет 300 символов в одну строку),
  `memory_delete(key, scope?)` (not-found — текстом, не исключением). Имена с
  подчёркиваниями (`memory_store`, не `memory.store`): точки в function-name несовместимы
  с OpenAI tool-calling — зафиксированное решение, действует для всех будущих скилов.
- **Промпт**: правило 10 — устойчивые факты и предпочтения пользователя сохранять через
  `memory_store` (scope=user; global — осторожно), перед персональными рекомендациями искать
  через `memory_search`, не дублировать инструкции/датасеты. Автоинъекция памяти в контекст
  агента — отдельной итерацией (шаг 7 «Порядка работ»).

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
- **instructions** (таблица модуля `instructions/`, см. выше): `id` (uuid str PK), `type`
  (knowledge|skill|tool, index), `title` (index), `content` (Text), `embedding` (JSON
  list[float]), `tags` (JSON list[str]), `version`, `usage_count`, `success_count`,
  `created_at`, `updated_at`; unique (`type`, `title`)
- **datasets** (таблица модуля `datasets/`, см. выше): `id` (uuid str PK), `owner_user_id`
  (str, index), `name`, `description`, `schema` (JSON: `{"fields": [...]}`), `usage_notes`,
  `retention`, `embedding` (JSON list[float]), `version`, `created_at`, `updated_at`;
  unique (`owner_user_id`, `name`)
- **dataset_records**: `id`, `dataset_id` FK → datasets.id (index), `owner_user_id`
  (str, index), `payload` (JSON), `created_at`; каскад удаления — явный DELETE в сторе
  (SQLite без PRAGMA foreign_keys)
- **memories** (таблица модуля `memory/`, см. выше): `id` (uuid str PK), `user_id`
  (str, NULLABLE = global, index), `key`, `content`, `tags` (JSON list[str]),
  `created_at`, `updated_at`; unique (`user_id`, `key`) — не действует для NULL owner'а,
  уникальность глобальных ключей обеспечивает стор (upsert select-then-update)

Все `*_at` — timezone-aware UTC: `UTCDateTime` (`db/base.py`) принудительно выставляет UTC при
чтении/записи (SQLite возвращает naive datetime). Создание схемы — `init_db` (`create_all`) в
lifespan; Alembic появится при первой деструктивной миграции.

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
- `core/tests/test_instructions_local.py` — контрактный набор фасада на `:memory:` (save/upsert
  с бампом версии и пересчётом эмбеддинга, get_by_name с сужением по типу, ранжирование:
  ближний вектор, буст точного title, k, инкремент usage_count, сидирование один раз);
  сервис строится фабрикой-фикстурой — тот же набор позже прогонит http-реализацию
- `core/tests/test_embeddings.py` — клиент эмбеддингов поверх мокнутого httpx-транспорта
  (форма запроса, разбор ответа с переупорядочиванием по index, ошибки статуса и payload)
- `core/tests/test_external_call.py` — парсинг/валидация ToolSpec, рендер шаблона с quoting,
  валидация параметров, whitelist-заголовок только для своего префикса, блок SSRF,
  редирект не следуется, обрезка тела
- `core/tests/test_ssrf_guard.py` — stub-resolver: private/loopback/link-local/multicast/CGNAT/
  IPv6 заблокированы, публичные разрешены, любой приватный из многих блокирует, неразрешимый
  хост и не-http(s) URL заблокированы
- `core/tests/test_instruction_skills.py` — адаптеры `instructions_search`/`instruction_save`/
  `external_call` (валидация аргументов + happy path на фейках)
- `core/tests/test_datasets.py` — контрактный набор фасада DatasetService на `:memory:`
  (create/get, дубликат имени, одно имя у разных owner'ов, изоляция, query: equals с
  тип-чувствительностью/диапазон дат/limit, delete каскадом со счётчиком, search с
  ранжированием и бустом точного имени); сервис строится фабрикой-фикстурой, как у instructions
- `core/tests/test_dataset_validation.py` — `parse_schema` (ок/ошибки, round-trip с
  `dump_schema`) и `validate_record` (все типы, required, bool ≠ int/number, лишние поля)
- `core/tests/test_data_skills.py` — `data_put` (создание со schema+description, отказ без
  них, запись в существующий, нарушения схемы текстом), `data_query` (JSON-строки, фильтры,
  лимиты, date-only границы, not-found текстом), `data_forget` (счётчик, not-found),
  `instructions_search` с datasets (merged-выдача с `[dataset]`)
- `core/tests/test_memory.py` — контрактный набор порта MemoryStore на `:memory:` (put
  create/upsert с сохранением id и бампом updated_at, get, изоляция owner'ов, глобальный
  скоуп виден всем, повторный put глобального ключа без дублей, delete, search: подстрока
  case-insensitive по key/content, литеральность LIKE-метасимволов, порядок updated_at DESC,
  limit, пустой query, tags round-trip); стор строится фабрикой-фикстурой, как у datasets
- `core/tests/test_memory_skills.py` — `memory_store`/`memory_search`/`memory_delete` на
  реальном сторе `:memory:` (scope-парсинг и default, ошибки аргументов, формат ответов,
  not-found тексты, изоляция и кросс-поверхностная видимость через SkillContext)
- `web/tests/test_config.py` — Settings: дефолты OF_EMBEDDING_*/top-k/whitelist/лимитов
  data_query и memory_search, парсинг JSON-whitelist и лимитов из env
- `web/tests/test_dialog_api.py` — get-or-create диалога, изоляция двух user_id, 400 без
  `X-User-Id`, messages/cancel/events (SSE через генератор), health, UI
- `web/tests/test_sse.py` — сериализация событий в SSE-кадры

План:

- `test_skills_engine.py`, auth-тесты

## Порядок работ

> Детальная дорожная карта по этапам A–F (БД → инструкции → датасеты → память →
> роутер → крон), включая решение отложить аутентификацию (user_id — доверенная
> строка от клиента), — в [plan.md](plan.md).

1. ✅ Скаффолд монорепо: библиотека `core/` + приложение `web/`, Makefile, ruff/mypy/pytest
2. ✅ Чат без аутентификации: UI, `POST /api/chat`, ядро — прокси к LLM
3. ✅ Петля + базовые скилы: `AgentLoop` (tool calling), `Skill/SkillSpec/SkillRegistry`, `http_request`
4. ✅ Событийная петля + актор диалога + фоновые задачи (in-memory): стриминг токенов (SSE), инъекции, отмена, `task_spawn`/`task_list`, проактивные уведомления, системный промпт answer-first
5. ✅ (без аутентификации: user_id — доверенная строка от клиента; users/токены отложены) БД (SQLAlchemy async, SQLite), перенос историй/задач в БД; диалоги keyed by (user, channel), поверхности — см. [dialogs.md](dialogs.md); две инсталляции (standalone/distributed) — см. [scaling.md](scaling.md)
6. Динамические скилы: Jinja-движок + `skill.save/run`; скилы памяти (user/global) ✅ (этап D)
7. Агентный контекст: память в контексте (автоинъекция), `GET /api/skills`, `GET /api/tasks`
8. LLM-роутер и процессная модель диалога — решения согласованы, см. [process-model.md](process-model.md); реализация отложена
9. Инструкции в БД (знание/скил/тул + векторный поиск, этап B ✅) — см. [instructions.md](instructions.md); датасеты пользовательских данных (этап C ✅) — см. [data-store.md](data-store.md); крон-задачи — см. [cron.md](cron.md); реализация после БД
10. Бэклог из обзора openclaw (SSRF-гвард, формула поиска, каталог скилов, детали крона и пр.) — см. [openclaw-review.md](openclaw-review.md)

## Проверка

- `make check` (ruff → mypy → pytest) — всё зелёное
- Ручной сценарий: `make run` → http://127.0.0.1:8000 → токены текут по мере генерации; «выполни GET к <url>» — шаг скилла виден в чате; «реши в фоне X» — агент подтверждает и продолжает диалог, результат приходит сам; «Стоп» — ответ обрывается; два разных имени — истории и задачи изолированы; перезапуск приложения не теряет диалог (история восстанавливается из БД)
- Целевой сценарий: два юзера → память не смешивается, скил общий, задачи разных юзеров изолированы
