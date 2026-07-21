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
  pyproject.toml               # deps: httpx, sqlalchemy[asyncio], aiosqlite, croniter;
                               #   dev: pytest, pytest-asyncio, ruff, mypy
  src/octoforge_core/
    domain.py                  # ChatMessage, ToolCall, MessageRole, Dialog
    config.py                  # LLMConfig, EmbeddingConfig
    time.py                    # utc_now() — единая точка времени (UTC aware)
    errors.py                  # LLMResponseError
    ports.py                   # Protocol-порты: LLMClient, TaskStore
    composition.py             # переиспользуемые builder'ы сборки (P5): build_llm_client,
                               #   build_instruction_service, build_dataset_service,
                               #   build_external_executor, build_skill_registry,
                               #   build_agent_loop, build_compactor, build_router,
                               #   build_cron_outcome_reporter, build_runner_config,
                               #   build_conversation_manager, build_cron_scheduler;
                               #   бандлы SkillLimits/SkillStores/SkillServices/RunnerOptions;
                               #   только порты и конфиги, без fastapi и web-Settings
    agent/
      events.py                # LoopEvent: IterationStarted, TextDelta, AssistantMessage,
                               #   ToolCallRequested/Completed/Failed, Finished, Cancelled, Failed,
                               #   ProcessSuspended/Resumed/Completed (маркеры актора)
      control.py               # LoopControl — mailbox инъекций + флаг отмены
      loop.py                  # AgentLoop.stream(history, control, context) → AsyncIterator[LoopEvent]
      router.py                # MessageRouter (Protocol), RouteAction/RouteOp/RouteDecision,
                               #   ProcessInfo/ProcessPlace, LLMRouter (one-shot tool call, таймаут, фолбэк)
      prompts.py               # порты промптов: PromptProvider (Protocol) + StaticPromptProvider
                               #   поверх вшитых DEFAULT_SYSTEM_PROMPT (answer-first, task_spawn,
                               #   уведомления, instructions_search / external_call / instruction_save,
                               #   data_put / data_query / data_forget,
                               #   memory_store / memory_search / memory_delete, крон, web_search)
                               #   и ROUTER_SYSTEM_PROMPT; имена SYSTEM/ROUTER_PROMPT_NAME
      runner.py                # ConversationRunner (актор: нарратив + процессы fg/bg, bound
                               #   TaskSpawner, wake для крон-выстрелов, порт
                               #   TaskOutcomeListener для исходов cron-задач),
                               #   ConversationManager, RunnerConfig, ConversationEvent
    skills/
      base.py                  # Skill (Protocol), SkillSpec, SkillOrigin (BASIC|DYNAMIC),
                               #   SkillContext (+ опциональный task_spawner)
      registry.py, errors.py   # реестр + ошибки
      basic/                   # http_request.py, task_spawn.py, task_list.py,
                               # instructions_search.py, instruction_save.py, external_call.py,
                               # data_put.py, data_query.py, data_forget.py,
                               # memory_store.py, memory_search.py, memory_delete.py,
                               # cron_jobs.py (cron_create/list/delete/pause/resume), web_search.py,
                               # history_search.py
    tasks/
      models.py                # Task, TaskKind (RUN), TaskStatus (PENDING|RUNNING|DONE|FAILED|CANCELLED)
      store.py                 # InMemoryTaskStore (реализация порта TaskStore, для тестов)
      spawner.py               # TaskSpawner (Protocol-порт спавна фоновых задач)
      errors.py                # TaskNotFoundError
    db/
      base.py                  # Declarative Base + UTCDateTime (aware UTC на чтении/записи)
      models.py                # ORM-модели: DialogRow, MessageRow, TaskRow
      engine.py                # create_engine/create_session_factory (DI) + bootstrap_schema
                               #   (Alembic upgrade/stamp) + init_db (create_all, fallback/тесты)
      migrations/              # Alembic env.py + baseline-ревизия (autogenerate из метаданных)
      repositories.py          # DialogRepository, MessageRepository, SqlAlchemyTaskStore
      errors.py                # DialogNotFoundError
    llm/
      events.py                # StreamEvent: TextDelta | StreamFinished
      openai.py                # OpenAI-совместимый клиент: complete() + stream() (SSE, tools)
      embeddings.py            # EmbeddingClient (Protocol-порт) + OpenAI-совместимый клиент
                               #   (POST /embeddings); порт общий для instructions/ и datasets/
      local_embeddings.py      # локальный бэкенд эмбеддингов: sentence-transformers bi-encoder
                               #   (как в b2e), L2-нормализация, вычисление в asyncio.to_thread
      reranker.py              # RerankerClient (Protocol-порт) + кросс-энкодер (MPS при наличии)
    instructions/              # обособленный модуль инструкций (только хранение/поиск/ранг)
      api.py                   # граница модуля: InstructionService (Protocol), Instruction,
                               #   InstructionType, SearchHit, InstructionNotFoundError,
                               #   порт хранилища InstructionStore + EmbeddedInstruction +
                               #   capability-порт InstructionVectorSearch (runtime_checkable)
      models.py                # InstructionRow — таблица instructions, собственность модуля
      store.py                 # SqlAlchemyInstructionStore (сессии через async_sessionmaker, DI)
      ranking.py               # чистые функции: cosine + буст точного title + реранк-мерж
      local.py                 # LocalInstructionService — локальная реализация фасада
                               #   (store инъектируется; vector-capable store → search_by_vector)
      seed.py                  # SEED_INSTRUCTIONS + seed_if_empty (generic http tool + скилы-примеры)
    datasets/                  # обособленный модуль датасетов (per-user трекеры, этап C)
      api.py                   # граница модуля: DatasetService (Protocol), Dataset, DatasetRecord,
                               #   DatasetSchema/FieldType, DatasetHit, ошибки модуля,
                               #   порт хранилища DatasetStore + EmbeddedDataset +
                               #   capability-порт DatasetVectorSearch (runtime_checkable)
      models.py                # DatasetRow + DatasetRecordRow — таблицы datasets/dataset_records
      validation.py            # parse_schema/dump_schema/validate_record (схема и записи)
      store.py                 # SqlAlchemyDatasetStore (явный каскад удаления)
      ranking.py               # свои чистые функции: cosine + буст точного имени (независимость)
      service.py               # LocalDatasetService — локальная реализация фасада
                               #   (store инъектируется; MAX_SCAN_ROWS)
    memory/                    # обособленный модуль памяти (per-user + global, этап D)
      api.py                   # граница модуля: MemoryStore (Protocol-порт), Memory,
                               #   MemoryScope (USER|GLOBAL), MemoryNotFoundError
      models.py                # MemoryRow — таблица memories, собственность модуля
      store.py                 # SqlAlchemyMemoryStore (upsert по (owner, key), LIKE-поиск)
    context/                   # обособленный модуль контекста: компакция нарратива + поиск по архиву
      api.py                   # граница модуля: ContextCompactor/SummaryStore/MessageArchive
                               #   (Protocol-порты), DialogueSummary/ArchivedMessage/ArchiveFilter
      models.py                # SummaryRow — таблица dialog_summaries, собственность модуля
      store.py                 # SqlAlchemySummaryStore (оба порта: саммари + read-only архив)
      compactor.py             # LlmContextCompactor (темы + горячий хвост, фоновая компакция
                               #   с guard'ом на диалог), NoopContextCompactor, CompactorConfig
      prompts.py               # промпт суммаризации + разбор ответа (TOPICS/SUMMARY)
    cron/                      # обособленный модуль крон-задач (этап F)
      api.py                   # граница модуля: CronStore/CronWaker/Scheduler (Protocol-порты),
                               #   CronJob, ошибки, compute_next_fire/count_missed
                               #   (croniter + zoneinfo) — публичный контракт для
                               #   альтернативных движков планирования
      models.py                # CronJobRow — таблица cron_jobs, собственность модуля
      store.py                 # SqlAlchemyCronStore (CRUD + due-выборка + CAS-аренда
                               #   + record_fire_result)
      scheduler.py             # CronScheduler (реализация порта Scheduler: asyncio-цикл
                               #   claim → wake → complete_fire, coalesce пропущенных,
                               #   разброс и лимит догонялки)
      reporter.py              # CronOutcomeReporter — исход процесса в стор: ретрай
                               #   с backoff'ом, удаление one-shot, сброс серии
      waker.py                 # ManagerCronWaker — адаптер порта на ConversationManager
    search/                    # обособленный модуль веб-поиска (порт провайдера)
      api.py                   # граница модуля: SearchProvider (Protocol), SearchResult,
                               #   SearchResponse, SearchError — транспорт-нейтральные DTO
      serper.py                # SerperSearchProvider — дефолтная реализация (serper.dev)
    net/                       # исполнение внешних вызовов (core-сторона, вне модуля)
      guard.py                 # SsrfGuard: resolve хоста (resolver инъектируется) → ipaddress-проверки;
                               #   allowed_prefixes для собственного base URL (пропуск до resolve)
      tool_spec.py             # ToolSpec + parse_tool_spec (JSON-формат tool-записи)
      external.py              # ExternalCallExecutor: шаблоны, whitelist-авторизация
                               #   (+ темплейт {user_id} в значении заголовка), SSRF-гвард
      errors.py                # SsrfBlockedError, ToolSpecError, ExternalCallError
    # дальше: skills/dynamic/ (Jinja-движок)
  tests/                       # test_agent_loop, test_composition, test_conversation_runner,
                               # test_cron_store,
                               # test_cron_scheduler, test_data_skills,
                               # test_datasets, test_datasets_store_port, test_dataset_validation,
                               # test_db_repositories, test_embeddings, test_external_call,
                               # test_http_request_skill, test_instruction_skills,
                               # test_instructions_local, test_instructions_store_port,
                               # test_memory, test_memory_skills,
                               # test_openai_client, test_openai_stream, test_prompts,
                               # test_router, test_serper_provider, test_skills_registry,
                               # test_ssrf_guard, test_tasks, test_web_search_skill
web/                           # приложение octoforge-web — FastAPI-обёртка
  pyproject.toml               # deps: octoforge-core, fastapi, uvicorn, pydantic-settings
  src/octoforge_web/
    main.py                    # app factory + composition root (DI поверх builder'ов
                               #   octoforge_core.composition: БД, LLM, эмбеддер, реестр,
                               #   исполнитель external_call, LLMRouter, акторы, крон-планировщик,
                               #   telegram-адаптер; канал "web")
    config.py                  # Settings (env с префиксом OF_, включая OF_DATABASE_URL,
                               #   OF_EMBEDDING_*, OF_INSTRUCTIONS_TOP_K,
                               #   OF_EXTERNAL_CALL_AUTH_WHITELIST, OF_DATASETS_QUERY_*,
                               #   OF_MEMORY_SEARCH_*, OF_MAX_PROCESSES, OF_ROUTER_TIMEOUT_SECONDS,
                               #   OF_SELF_BASE_URL, OF_CRON_*, OF_TELEGRAM_*, OF_SERPER_TOKEN,
                               #   OF_SYSTEM_PROMPT_SOURCE / OF_ROUTER_PROMPT_SOURCE)
    prompts.py                 # FilePromptProvider — промпты из файлов (file:), fallback
                               #   на StaticPromptProvider; перечитывает файл на каждый get()
    deps.py                    # провайдеры зависимостей из app.state + заголовок X-User-Id
    api/
      dialog.py                # messages/cancel/events(SSE) по (user_id, channel)
      cron.py                  # cron jobs CRUD + pause/resume (query-параметры, X-User-Id)
      sse.py                   # сериализация LoopEvent → SSE-кадры (включая маркеры процессов)
      schemas.py               # pydantic-схемы запросов/ответов
    telegram/                  # Telegram-адаптер (этап G): вторая поверхность, канал "telegram"
      models.py                # pydantic-модели Bot API (update/message/chat/user, extra=ignore)
      client.py                # порт TelegramClient + TelegramBotClient на httpx (getUpdates,
                               #   sendMessage, editMessageText, sendChatAction)
      bridge.py                # TelegramBridge: события runner'а → черновик с throttle-правками,
                               #   чанкер 4096, статус-строки скилов; текст → runner.submit
      markdown.py              # markdown → Telegram-HTML (parse_mode=HTML) + split_html_safe
      poller.py                # TelegramPoller (long-poll, offset, backlog-drain, backoff) +
                               #   TelegramBridgeRegistry (get-or-create + прогрев из БД)
      __main__.py              # standalone-запуск: только Telegram-адаптер, без HTTP API
                               #   (python -m octoforge_web.telegram; порт не слушается)
    static/index.html          # чат-UI: SSE-стрим, шаги скилов, маркеры процессов,
                               #   кнопка «Стоп», поле имени (= user_id)
  tests/                       # test_dialog_api.py, test_cron_api.py, test_sse.py, test_config.py,
                               # test_seed.py, test_prompts.py, test_modularity.py,
                               # test_telegram_models.py, test_telegram_bridge.py,
                               # test_telegram_poller.py, test_telegram_client.py,
                               # test_telegram_standalone.py, test_telegram_markdown.py
```

## Петля агента: события, управление, актор

### Петля как поток событий (`octoforge_core/agent/loop.py`)

`AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` — не «возвращает ответ»,
а выдаёт поток событий (`agent/events.py`):

- `IterationStarted(index)` — начало итерации рассуждения;
- `TextDelta(text)` — токен ответа по мере стриминга LLM;
- `AssistantMessage(message, interrupted)` — завершённое сообщение итерации (interrupted — обрыв по отмене);
- `ToolCallRequested / ToolCallCompleted(output) / ToolCallFailed(error)` — шаги скилов;
- `Finished(message)` — финальный ответ; `Cancelled` — отмена; `Failed(error)` — срыв без ответа;
- `ProcessSuspended / ProcessResumed(process_id, title)` и
  `ProcessCompleted(process_id, title, status)` — маркеры уровня актора (не петли):
  форграунд ушёл в фон / фон стал форграундом / процесс завершился со статусом
  (значение TaskStatus); эмитятся актором, в union `LoopEvent` входят для общего канала доставки.

`history` мутируется на месте (в неё дописываются новые сообщения) — владелец списка (актор)
забирает накопленное в историю диалога.

**Eager-исполнение тулов.** Стрим LLM несёт инкрементальные события тул-коллов
(`llm/events.py`): `ToolCallStarted` (появились id+имя слота), `ToolCallReady` (аргументы
достриманы и распарсились), `ToolCallBroken` (JSON не собрался). На `Ready` петля сразу
запускает исполнение (`asyncio.create_task`, событие `ToolCallRequested`), не дожидаясь
конца сообщения; результаты сливаются через очередь и эмитятся по мере готовности.
В историю tool-сообщения пишутся строго в порядке вызовов после assistant-сообщения
(детерминизм при конкурентном исполнении). `Broken` не исполняется: в его очередь —
`ToolCallFailed` и tool-сообщение `error: ...`. Провайдер без инкрементальных дельт
(ни одного `Ready`/`Broken` за стрим) — fallback: вызовы финального сообщения стартуют
после `StreamFinished`, как раньше. Владелец задач — внутренний трекер петли
(`_ToolRunTracker`: spawn/дрен/ожидание/отмена).

**Idle-watchdog стрима.** Пауза между событиями стрима (включая ожидание первого)
ограничена таймаутом `stream_idle_timeout` (параметр `AgentLoop`, `None` = выключен;
конфиг `OF_LLM_STREAM_IDLE_TIMEOUT_SECONDS`, дефолт 120, 0 = выключен). Таймаут обрывает
стрим (`aclose`), отменяет запущенные тулы и завершает прогон `Failed("LLM stream idle
timeout")`. Подробности — [streaming.md](streaming.md).

### Управление прогоном (`agent/control.py`)

`LoopControl`: mailbox (`asyncio.Queue`) для инъекций + флаг отмены. Точки обработки:

- перед каждым вызовом LLM — дрейн mailbox в историю (инъекции пользователя и уведомления задач);
- на каждый чанк стрима — проверка отмены: стрим обрывается (`aclose`), запущенные тулы
  отменяются, частичный текст сохраняется как `AssistantMessage(interrupted=True)` с пришедшими
  tool_calls (на каждый — tool-ответ «cancelled» или реальный результат, если успел),
  эмитится `Cancelled`.

Инъекция никогда не вклинивается между assistant(tool_calls) и его tool-результатами —
только на безопасных границах итераций.

### Актор диалога: нарратив и процессы (`agent/runner.py`)

`ConversationRunner` — актор на диалог: единый inbox команд (`_Submit`, `_Cancel`,
`_ProcessTerminated`) сериализует всё, что происходит с диалогом. Владеет **нарративом**
(in-memory, при создании пересобранным из БД) и **процессами** (`dict[id, _Process]`,
один форграунд), ведёт подписчиков (очереди для SSE-broadcast, seq нумерация событий).
Модель и правила — [process-model.md](process-model.md).

- **Процесс** — обработка одного вопроса: свой прогон петли со своей веткой истории
  (`[system prompt] + копия нарратива` на момент старта; ветка дальше живёт своей жизнью).
  Форграунд ровно один: его события петли broadcast'ятся подписчикам (проверка места на
  каждое событие — место может смениться mid-run); фоновые работают молча. Маркеры
  `ProcessSuspended`/`ProcessResumed`/`ProcessCompleted` broadcast'ятся всегда.
- **Нарратив** = user-сообщения + финальные ответы завершённых процессов + system-
  уведомления (о задачах, об отказах по лимиту) + salvaged-фрагменты прерванных ответов.
  Только нарратив персистится в `messages` (user — при submit до роутинга; финалы и
  заметки — по факту); промежуточные assistant/tool-сообщения ветки не персистятся.
  Диалог переживает перезапуск за счёт пересборки нарратива; **процессы — нет**
  (in-memory; регрессия-допущение: задачи PENDING/RUNNING после рестарта осиротевают).
- **submit**: user-сообщение → нарратив + персист → снимок процессов →
  `router.route(snapshot, message, max_processes)` → пакет операций применяется по
  порядку (пустой пакет ≡ `[START_NEW]`). Операции: `INJECT` (в форграунд; fg свободен →
  семантика START_NEW), `START_NEW` (занятый fg уходит в фон с `ProcessSuspended`),
  `PROMOTE(target)` (bg → fg с `ProcessResumed`), `CANCEL(target)` (fg-слот освобождается
  по факту терминации, автовозврата нет).
- **Guardrail лимита** (детерминированный, `OF_MAX_PROCESSES`): перед START_NEW/PROMOTE —
  `активных − отменённых этим пакетом + 1 > max` → операция не выполняется; вместо неё
  system-заметка «лимит достигнут, предложи отменить одну из: <titles>» — инъекция в fg
  (занят) или репорт-прогон (свободен). User-сообщение остаётся в нарративе.
- **Репорт-прогон** — обычный fg-процесс с title="report" поверх нарратива, где последнее
  сообщение — system-заметка; стартует только на свободном fg (guardrail не применяется).
  Ветка завершается user-nudge'ом («кратко сообщи результат пользователю»): на хвост из
  system-заметки часть моделей отвечает пустотой (весь вывод уходит в reasoning-поле).
- **Текущая дата в системном промпте**: ветки процессов (fg и bg) получают системный промпт
  с суффиксом текущей даты/времени UTC (`_with_current_date`) — иначе модель гадает год.
- **Завершение процесса**: `Finished` → финал в нарратив + персист (task-backed →
  `mark_done`); `Failed` → task-backed → `mark_failed`; отмена → task-backed →
  `store.cancel` + гигиена: прерванное assistant-сообщение с непустым текстом из хвоста
  ветки + system-заметка о неполноте дописываются в нарратив. Затем: процесс убирается,
  broadcast `ProcessCompleted(status)` (значения TaskStatus), в inbox —
  `_ProcessTerminated`; недочитанные инъекции (`control.drain()`) возвращаются в inbox
  как `_Submit(recorded=True)` (уже в нарративе, повторно не персистятся). Финализация
  обёрнута в `try/finally` (`_pump_process`): даже при сбое записи в стор процесс всегда
  убирается из `_processes` и слот `max_processes` освобождается.
- **Уведомление о задаче** (обработка `_ProcessTerminated`): task-backed процесс
  завершился DONE/FAILED и результат ещё не доставлен → `mark_delivered` + system-
  уведомление с результатом → нарратив + персист → инъекция в fg (занят) или репорт-
  прогон (свободен). Отменённые задачи не уведомляют.
- `cancel()` (web API) отменяет только форграунд; `stop()` — все процессы и сам актор.
- **Супервизия и наблюдаемость**: цикл актора (`_run_actor`) ловит исключения обработки
  команды и логирует их — одна сбойная команда (например, ошибка стора в submit) не
  превращает диалог в зомби; `add_done_callback` логирует неожиданный выход актора.
  Ранее немые `except` (краш петли, сбой финализации, сбой доставки cron-wake) теперь
  логируются; потеря SSE-события по `QueueFull` считается (`_dropped_events`), а не
  молча глотается. В `core/` есть логгеры модулей (`runner`, `cron/scheduler`).
- `ConversationManager` — реестр runner'ов по dialog_id (создание под lock'ом),
  get-or-create диалога по (user_id, channel); конструктор принимает `RunnerConfig`
  (loop, prompts, router, max_processes, task_outcome_listener) + репозитории. Канал для ядра —
  непрозрачная строка; конкретные значения (`"web"`, будущий `"telegram"`) объявляют
  адаптеры в composition root.
- **Компакция нарратива**: ветка процесса собирается компактором из
  `RunnerConfig.compactor` (порт `ContextCompactor`): блок тем (все саммари
  диалога одним system-сообщением) + горячий хвост (`seq > max(seq_to)`,
  дословно); при переполнении хвоста (`OF_CONTEXT_HOT_MAX_CHARS`) стартует
  фоновая компакция — старейшие сообщения хвоста одним LLM-вызовом в запись
  `dialog_summaries` (guard «одна компакция на диалог», фейл = warning-лог).
  Модуль `context/` + скил `history_search` — см. [context.md](context.md).

### Роутер сообщений (`agent/router.py`)

Каждое входящее сообщение проходит `MessageRouter` (Protocol):
`route(processes, message, max_processes) -> RouteDecision` — пакет `RouteOp`
(`RouteAction`: INJECT | START_NEW | CANCEL | PROMOTE; target_id обязателен у
CANCEL/PROMOTE). Пустой пакет — passthrough (актор трактует как `[START_NEW]`).
`ProcessInfo` — снимок активного процесса (id, title, place: FOREGROUND|BACKGROUND).

Первая реализация — `LLMRouter(llm, timeout_seconds, prompts)`: пустой снимок → passthrough без
вызова LLM; иначе one-shot `complete()` с tool `route(ops)` (системный промпт-шаблон приходит
из `PromptProvider` — `ROUTER_PROMPT_NAME`, плейсхолдеры `{limit}`/`{processes}`; со списком
процессов и правилами: при активном форграунде дефолт — inject, start_new только для очевидно
несвязанного вопроса; сообщение пользователя — отдельным user-сообщением) под `asyncio.wait_for`.
Нет tool_call, ошибка или таймаут → фолбэк: есть форграунд → `[INJECT]`, иначе пусто.
Невалидные операции (неизвестный action, лишний/отсутствующий target, target не из снимка)
отбрасываются; пакет с INJECT теряет все START_NEW (детерминированный guardrail против увода
вопроса в фон); валидный остаток — решение (может стать пустым).

### Системный промпт (`agent/prompts.py`)

Промпты ядра поставляются через порт `PromptProvider` (`get(name) -> str`; имена —
`SYSTEM_PROMPT_NAME`/`ROUTER_PROMPT_NAME`). Дефолт — `StaticPromptProvider` поверх вшитых
констант; web-слой оборачивает его в `FilePromptProvider` (`web/prompts.py`): имена с
настроенным `file:`-источником (`OF_SYSTEM_PROMPT_SOURCE`/`OF_ROUTER_PROMPT_SOURCE`)
читаются из файла на каждый `get()` (правка файла действует без рестарта), нечитаемый файл
или ненастроенное имя — fallback на вшитый дефолт (warning в лог). Ядро env не читает.
`RunnerConfig` держит провайдер (а не строку): системный промпт подставляется в ветку
процесса при старте с суффиксом текущей даты UTC (`_with_current_date`).

Текст вшитого `DEFAULT_SYSTEM_PROMPT`: отвечать «сначала суть, потом детали» (прерывание полезно),
работа «в фоне прямо сейчас» (результат один раз, когда готов) → `task_spawn` и продолжить
диалог, при system-уведомлении о задаче — коротко
сообщить результат, для HTTP — `http_request`, статусы задач — `task_list`; перед
нетривиальной задачей — `instructions_search` (знания/сценарии/тулы/датасеты), вызов найденных
тулов — через `external_call`, после нового многошагового сценария — сохранить его
через `instruction_save`; трекинг структурированных данных (еда, вес, привычки) —
датасеты: искать через `instructions_search`, писать через `data_put` (создавая датасет
со схемой при отсутствии), читать/строить отчёты через `data_query`, удалять через `data_forget`;
просьбы «по расписанию/периодически/напоминай» (включая разовые «через час») — скил
`cron_create` (cron-выражение составить самому, таймзону уточнить или взять UTC; разовые —
`one_shot=true` с датированным выражением, задача удалится сама после срабатывания;
явная граница с `task_spawn`: напоминания — только крон), управление — `cron_list`/`cron_pause`/
`cron_resume`/`cron_delete`, подтвердив создание пользователю; факты из веба — скил
`web_search`; разметка ответов — простая (`**bold**` для акцентов и заголовков, списки
дефисом, код в fenced-блоках, таблицы избегать) — рендерится и в web, и в Telegram.

### Стриминг LLM (`llm/openai.py`)

`complete()` — обычный (non-streaming) вызов, используется LLM-роутером (one-shot с tool call).
`stream()` — `stream=true`: парсинг SSE-чанков (delta.content → `TextDelta`,
delta.tool_calls — аккумуляция по index со склейкой arguments). Слот закрывается при
переходе дельт на следующий index (последний — на `finish`): валидные аргументы →
`ToolCallReady`, битый JSON → `ToolCallBroken` (в финальном сообщении такой вызов остаётся
с пустыми аргументами — стрим не падает). В конце — `StreamFinished(message)`, источник
истины для истории. `aclose()` на генераторе обрывает HTTP-соединение — основа отмены.

## Фоновые задачи

Задача — это фоновый процесс актора, подкреплённый записью в `TaskStore`
(исполнение = pump-процесс; глобальный поллер упразднён).

- `tasks/models.py`: `Task` (id, dialog_id, user_id, channel, title, kind, input, status,
  result, error, result_delivered, created_at/started_at/finished_at — через `utc_now()`);
  `TaskKind(RUN)`, `TaskStatus(PENDING|RUNNING|DONE|FAILED|CANCELLED)`. `channel` и `user_id`
  денормализованы в задачу: уведомление уходит на поверхность, с которой задача запущена,
  без join'а к dialogs.
- `TaskStore` (Protocol в `ports.py`): add/get/list(dialog_id)/mark_running/mark_done/
  mark_failed/cancel/is_cancelled/count_active/mark_delivered. Реализации:
  `SqlAlchemyTaskStore` (`db/repositories.py`, боевой путь) и `InMemoryTaskStore` (тесты);
  `next_pending` из порта удалён вместе с поллером.
- **Спавн**: скил `task_spawn` делегирует порту `TaskSpawner` (`tasks/spawner.py`),
  который актор биндит на диалог (`SkillContext.task_spawner`, опциональный — контексты
  вне актора обходятся без него). Спавнер проверяет лимит процессов (отказ текстом),
  создаёт `Task(kind=RUN, input={title, prompt})`, сразу `mark_running` и поднимает
  bg-процесс с id = task.id, коротким системным промптом фоновой задачи и
  user-сообщением из prompt. Спавн — всегда в фон (вызывается из работающего fg).
- **Завершение**: терминальный статус процесса мапится на задачу (DONE → mark_done с
  финальным ответом, FAILED → mark_failed, отмена → cancel). Уведомление в диалог —
  по факту терминации (см. «Актор диалога»), ровно один раз (`result_delivered`).
- **Регрессия-допущение**: процессы живут в памяти — после рестарта приложения задачи в
  статусах PENDING/RUNNING осиротевают (их процессы не восстанавливаются). Редоставка
  недоставленных результатов и реанимация/фейл осиротевших задач — в списке
  «не реализовано» (см. AGENTS.md).

Скилы задач: `task_spawn` (title + prompt → фоновая задача-процесс текущего диалога) и
`task_list` (задачи диалога со статусами, включая cancelled, и результатами).

## Скилы

> **Модель уточнена**: скилы стали одним из типов инструкций в общем хранилище (знание/скил/тул)
> с векторным поиском — см. [instructions.md](instructions.md). Типизация BASIC/DYNAMIC отменена;
> раздел ниже описывает состояние на момент реализации.

Скил — единица, которую агент вызывает через LLM tool calling. Два типа:

- **Базовые (`SkillOrigin.BASIC`)** — код проекта (`octoforge_core/skills/basic/`): `http_request`,
  `task_spawn`, `task_list`, `instructions_search`, `instruction_save`, `external_call`,
  `data_put`, `data_query`, `data_forget`, `memory_store`, `memory_search`, `memory_delete`,
  `cron_create`, `cron_list`, `cron_delete`, `cron_pause`, `cron_resume`, `web_search`.
  Подключаются в composition root. Имена скилов — с подчёркиваниями, не с точками:
  точки в function-name несовместимы с OpenAI tool-calling (зафиксированное решение).
- **Динамические (`SkillOrigin.DYNAMIC`)** — Jinja-шаблоны из БД (появятся с БД). Реестр един:
  `SkillRegistry` хранит скилы обоих типов под уникальными именами.

Абстракция (`skills/base.py`): `SkillSpec` (name, description, parameters_schema — JSON Schema);
`Skill` (Protocol): `spec` + `async execute(arguments, context) -> str`;
`SkillContext` — per-invocation контекст (user_id, channel, dialog_id + опциональный
`task_spawner: TaskSpawner | None` — None вне актора, `task_spawn` тогда отказывает).
Аргументы валидирует сам скил (`SkillArgumentsError`).

Скил `web_search` зависит от порта `SearchProvider` (модуль `search/`: транспорт-нейтральные
DTO `SearchResponse`/`SearchResult`, ошибка `SearchError`), а не от конкретного поисковика:
дефолт — `SerperSearchProvider` (serper.dev, регистрируется при `OF_SERPER_TOKEN`),
инсталлятор подставляет свой провайдер (Bing/Brave/Tavily) в composition root. Форматирование
выдачи, клэмп `num_results` (1..10) и срез вывода — ответственность скилла.

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
  JSON-совместимые под будущую HTTP-границу; **порт хранилища `InstructionStore`** — CRUD +
  `list_with_embeddings` — и runtime-checkable capability `InstructionVectorSearch` для
  сторов с поиском на своей стороне, напр. pgvector). Локальная реализация
  `LocalInstructionService` получает стор конструктором (дефолт —
  `SqlAlchemyInstructionStore`, таблица `instructions` — собственность модуля), эмбеддинг
  `title + "\n" + content` через порт `EmbeddingClient` (два бэкенда: OpenAI-совместимый
  `llm/embeddings.py` и локальный sentence-transformers `llm/local_embeddings.py`; выбор —
  `OF_EMBEDDING_BACKEND`), ранжирование — brute-force cosine + буст точного `title`
  (`ranking.py`, чистые функции; полная формула 70/30 + MMR — позже подменой модуля) +
  опциональный реранк шортлиста кросс-энкодером (`OF_RERANKER_MODEL`; двухстадийная схема
  как в b2e: cosine-шортлист `rerank_candidates` → cross-encoder → top-k). Если стор
  реализует `InstructionVectorSearch`, сервис делегирует ему выбор кандидатов
  (`search_by_vector`) вместо полного скана таблицы; буст и реранк остаются на сервисе.
  `search` инкрементирует `usage_count` возвращённых хитов.
  Сидирование `seed_if_empty` (generic weather tool + два скила-примера) — в lifespan;
  запускается при `embeddings_configured()` (local-бэкенд или заданный ключ); падение
  сидирования не роняет старт — warning в лог, приложение работает без сидов.
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
  `delete_dataset`/`search`, DTO JSON-совместимые под будущую HTTP-границу; **порт хранилища
  `DatasetStore`** — дескрипторы/записи/`list_with_embeddings` — и runtime-checkable
  capability `DatasetVectorSearch` для сторов с поиском на своей стороне). Локальная
  реализация `LocalDatasetService` получает стор конструктором (дефолт —
  `SqlAlchemyDatasetStore`, таблицы `datasets` + `dataset_records` — собственность модуля),
  эмбеддинг дескриптора `name + "\n" + description + "\n" + usage_notes` через общий порт
  `EmbeddingClient` (`llm/embeddings.py`, перенесён туда из `instructions/`), ранжирование —
  brute-force cosine + буст точного имени (свой `ranking.py`, от instructions не зависит);
  vector-capable стор получает `search_by_vector(owner, embedding, k)` вместо полного скана.
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

## Крон-задачи (этап F)

Модель, надёжность и принятые решения — в [cron.md](cron.md). Реализация:

- **Модуль `cron/`** — обособленный, зеркалит `datasets/` и `memory/`: граница `api.py`
  (Protocol `CronStore` — CRUD, due-выборка `list_due`, CAS-аренда `claim`/`release_claim`/
  `complete_fire`, исход `record_fire_result`; Protocol `CronWaker`; Protocol `Scheduler`
  — порт движка планирования; DTO `CronJob` (+ `one_shot`, `last_status`, `last_error`,
  `retry_count`); ошибки `CronJobNotFoundError`/
  `CronScheduleError`; чистые функции расписания `compute_next_fire`/`count_missed` на
  croniter + zoneinfo — новая зависимость `croniter`; без py.typed → локальный
  `ignore_missing_imports` с комментарием в `core/pyproject.toml`). Локальная реализация
  `SqlAlchemyCronStore`: таблица `cron_jobs` (собственность модуля). Расписание считается
  croniter'ом по IANA-таймзоне задачи; хранение и сравнения — aware UTC.
  `CronStore.list_due`/`claim`/`release_claim`/`complete_fire` + `compute_next_fire`/
  `count_missed` — публичный контракт для альтернативных движков (Celery beat, APScheduler,
  OS cron): инсталлятор либо подменяет `Scheduler` в корне, либо не стартует наш и драйвит
  store + математику расписаний из своего раннера.
- **Планировщик** (`cron/scheduler.py`): `CronScheduler` — реализация порта `Scheduler`,
  asyncio-задача в lifespan, `owner` = uuid инстанса, knobs из Settings через
  `CronSchedulerConfig`. Тик (метод `tick(now)` тестируем без сна): due-выборка (enabled,
  `next_fire_at <= now`, аренда свободна или протухла по lease TTL, ORDER BY `next_fire_at`,
  LIMIT replay_limit) → CAS `claim` одним UPDATE (проигравший гонку пропускает) → wake
  (prompt + суффикс про пропущенные прогоны при coalesce) → `complete_fire` с пересчётом
  `next_fire_at` от момента выстрела; исключение из waker → `release_claim`, задача остаётся
  due. Между выстрелами тика — разброс 0.5 с (константа `REPLAY_STAGGER_SECONDS`).
- **Исход выстрела** (`cron/reporter.py` + хук в акторе): актор репортит терминальный
  статус задачи с `cron_job_id` через порт `TaskOutcomeListener` (`agent/runner.py`,
  generic; ошибки репортёра ловятся — диалог не страдает). Адаптер `CronOutcomeReporter`
  применяет политику: DONE → сброс серии ретраев (+ удаление one-shot задачи), FAILED →
  ретрай с экспоненциальным backoff'ом (`OF_CRON_RETRY_LIMIT`=3,
  `OF_CRON_RETRY_BACKOFF_SECONDS`=60 → 1/2/4 мин) через `record_fire_result` (атомарный
  UPDATE: `last_status`/`last_error` ≤ 500 символов, сдвиг `next_fire_at`, инкремент
  `retry_count`), лимит исчерпан → фиксация фейла, задача живёт по расписанию; CANCELLED →
  без ретрая. Колонки приезжают Alembic-ревизией `2b8f4c1a9e07`; `bootstrap_schema` теперь
  явно коммитит транзакцию миграций (баг: неявный откат терял version-запись и ALTER'ы).
  Добавление колонок в ревизии условное (пропуск уже существующих): legacy-БД штампуется
  на baseline даже когда её `create_all` был новее и колонки уже есть (баг: stamp на head
  помечал такую БД актуальной, и ALTER'ы никогда не применялись).
- **One-shot напоминания**: флаг `one_shot` в `cron_jobs` и `cron_create` (и в HTTP
  `POST /jobs`); расписание — датированное cron-выражение (`minute hour day month *`),
  агент строит сам под ближайшее будущее вхождение; после первого DONE задача удаляется.
- **Идемпотентность `cron_create`**: совпадение `(title, schedule, prompt, one_shot)` по
  `list_for_user` → ответ `already exists` вместо дубля. `cron_list` и HTTP API показывают
  `last run: <status> (<error>)` и `retry #N`.
- **Выстрел в акторе**: `ConversationManager.wake` → get-or-create runner →
  `ConversationRunner.wake(title, prompt, cron_job_id)` — общий со `spawn_task` приватный
  хелпер, но `Task.input += {"cron_job_id"}`; переполнение лимита процессов → системная
  заметка `CRON_LIMIT_NOTE_TEMPLATE` в нарратив (через `_publish_system_note`), а не
  текст-отказ. Уведомление о результате — существующий путь завершения фоновой задачи.
- **Своё API как внешнее**: SSRF-гвард += `allowed_prefixes` (пропуск до resolve; в
  composition root — только `OF_SELF_BASE_URL`); `ExternalCallAuth.header_value` +=
  темплейт `{user_id}` (подстановка из `SkillContext.user_id`; вызов без user_id → без
  заголовка); скил `external_call` передаёт `context.user_id`. В whitelist composition
  root программно добавляется запись `(self_base_url, "X-User-Id", "{user_id}")`.
- **Нативные скилы** (`skills/basic/cron_jobs.py`): `cron_create`/`cron_list`/
  `cron_delete`/`cron_pause`/`cron_resume` над `CronStore` — семантика HTTP-эндпоинтов
  (owner-скоуп, resume пересчитывает `next_fire_at` от now), но без loopback-вызовов:
  работают и в standalone Telegram-раннере. Регистрируются в composition root на всех
  поверхностях; agent-managed крон больше не зависит от поднятого HTTP API.
- **HTTP API** (`web/api/cron.py`, префикс `/api/cron`, скоуп по `X-User-Id`, параметры —
  query string): `POST /jobs` (201; валидация schedule через croniter и timezone через
  zoneinfo → 422 с detail; `next_fire_at` от now), `GET /jobs`, `DELETE /jobs/{id}`
  (204; чужая/нет → 404), `POST /jobs/{id}/pause|resume`. Остаётся для внешних клиентов.
- **Сид**: `migrate_cron_tools_to_native(service)` — удаляет HTTP-сид-тулы крона
  (`cron_create_job` и др.) через `InstructionService.delete` и обновляет скил-сценарий
  `schedule_a_recurring_report` на нативные скилы; идемпотентна, вызов в lifespan под
  тем же условием рабочих эмбеддингов.
- **Промпт**: правило 11 — см. «Системный промпт».

## Telegram-адаптер (этап G)

Решения о поверхностях — в [dialogs.md](dialogs.md). Реализация
(`web/src/octoforge_web/telegram/`; core про транспорт не знает):

- **Транспорт**: Bot API напрямую через httpx (без aiogram): `TelegramBotClient` (порт
  `TelegramClient`) — `getUpdates` (long poll), `sendMessage`, `editMessageText`,
  `sendChatAction`; `ok=false` → `TelegramApiError`, «message is not modified» глушится
  в правках. Модели (`telegram/models.py`) — pydantic, `extra="ignore"`, алиас `from`;
  тип чата — `TelegramChatType(StrEnum)`.
- **Поверхность**: канал `"telegram"` объявлен адаптером; `user_id = "tg:<telegram user id>"`;
  только личные чаты (группам и не-текстовым сообщениям — короткое уведомление).
  Идентичности web/telegram не связываются (alice ≠ tg:123) — линкинг придёт с
  аутентификацией; память per-user работает внутри каждой идентичности.
- **Поллер** (`telegram/poller.py`): цикл long-poll в lifespan-задаче; offset в памяти;
  при старте backlog сливается (`offset=-1`), старые сообщения не реплеятся; httpx/API-ошибки —
  лог + backoff, цикл живёт; «ядовитый» update редуцируется до голого `update_id`, чтобы
  offset не встал на нём. Команды: `/start` (приветствие), `/cancel` (отмена прогона).
- **Мост** (`telegram/bridge.py`): `TelegramBridge` на чат: постоянная подписка на события
  runner'а (подписка ДО submit — события не реплеятся), рендер в одно «черновое» сообщение:
  дельты текста с throttle-правками (`OF_TELEGRAM_EDIT_THROTTLE_SECONDS`), статус-строки
  скилов (⚙️/⚠️) и маркеры ухода/возврата процессов (⏸️/▶️) в порядке прихода;
  `ProcessCompleted` не рисуется (завершения приходят текстом репорт-прогона). Переполнение
  лимита 4096 — seal текущего сообщения и продолжение в новом (запечатанные головы в
  `_Draft.sealed_chunks`, буфер копится сырым). Зависимость
  моста — `RunnerProvider` (callable → runner); в composition root это
  `ConversationManager.get_or_create_runner`.
- **Разметка** (`telegram/markdown.py`): ответы модели идут с `parse_mode="HTML"`: сырой
  markdown буфера конвертируется в Telegram-HTML (`markdown_to_telegram_html` — bold/italic/
  strike, inline/fenced code, ссылки, заголовки как `<b>`, цитаты, списки `•`, экранирование),
  разбиение — `split_html_safe` (срез по границе строки/слова, никогда внутри тега; стек
  открытых тегов закрывается в голове и переоткрывается в хвосте). На ошибку Bot API
  «can't parse entities» клиент повторяет отправку без parse_mode (plain text-фолбэк).
  Rich Messages из Bot API 10.1 (sendRichMessage/Draft) осознанно не используем: свежее
  API, наш поток построен на editMessageText.
- **Прогрев**: при старте мосты поднимаются для всех диалогов канала telegram из БД
  (`DialogRepository.list_user_ids_by_channel`) — иначе крон-выстрелы и уведомления задач
  после рестарта ушли бы в пустоту (подписчиков нет); chat_id выводится из `tg:<id>`.
- **Конфиг**: `OF_TELEGRAM_BOT_TOKEN` (пусто = адаптер выключен),
  `OF_TELEGRAM_POLL_TIMEOUT_SECONDS` (30), `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` (1.5).
- **Standalone-режим** (`telegram/__main__.py`): `python -m octoforge_web.telegram` поднимает
  только адаптер и крон-планировщик на общем composition root'е (`runtime()` в `main.py`,
  вынесен из FastAPI-lifespan) — FastAPI-приложение не создаётся, порт не слушается, только
  исходящие соединения (Bot API, LLM, эмбеддинги). Без токена процесс отказывается стартовать;
  остановка по SIGINT/SIGTERM.
- **Токен в логах**: HTTP-статусы Bot API конвертируются в `TelegramApiError` без URL
  (`raise ... from None`) — токен из пути запроса не попадает в логи.
- **Не входит**: группы/треды, webhook-режим, медиа и файлы, inline-кнопки,
  связывание идентичностей, outbox при оффлайн-инстансе ([scaling.md](scaling.md)),
  Rich Messages (Bot API 10.1+), таблицы в разметке (промпт просит код-блоки).

## Модель данных

ORM-модели — в `octoforge_core/db/models.py`; доменные объекты — в `octoforge_core/domain.py`
и `octoforge_core/tasks/models.py`; маппинг ORM↔домен — в репозиториях
(`octoforge_core/db/repositories.py`). Таблицы `users` пока **нет**: `user_id` — доверенная
непрозрачная строка от клиента, аутентификация отложена (см. [plan.md](plan.md)). Схема:

- **dialogs**: `id` (uuid str PK), `user_id` (str, index), `channel` (str), `created_at`,
  `updated_at`; unique (`user_id`, `channel`)
- **messages**: `id`, `dialog_id` FK (index), `seq` (int, монотонно растёт в рамках диалога;
  unique (`dialog_id`, `seq`); присваивается подзапросом `max(seq)+1` прямо в INSERT —
  конкурирующие писатели, актор и pump'ы процессов, не конфликтуют), `role` (значение
  MessageRole), `content`, `tool_calls` (JSON, nullable), `tool_call_id` (nullable),
  `created_at`. Пишется только нарратив (см. «Актор диалога»), не полные ветки прогонов
- **tasks**: `id`, `dialog_id` FK (index), `user_id` (index), `channel`, `kind`
  (RUN), `title`, `input` (JSON: `{"title", "prompt"}`), `status` (PENDING|RUNNING|DONE|
  FAILED|CANCELLED), `result`, `error`, `result_delivered` (bool, default False),
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
- **cron_jobs** (таблица модуля `cron/`, см. выше): `id` (uuid str PK), `user_id`
  (str, index), `channel`, `title`, `schedule` (cron-выражение), `timezone` (IANA),
  `prompt`, `enabled` (bool), `next_fire_at`, `last_fire_at` (nullable), `claimed_by`
  (nullable), `claimed_at` (nullable), `created_at`; индекс (`enabled`, `next_fire_at`)
  для due-выборки; пара (`claimed_by`, `claimed_at`) — аренда планировщика (lease TTL)

Все `*_at` — timezone-aware UTC: `UTCDateTime` (`db/base.py`) принудительно выставляет UTC при
чтении/записи (SQLite возвращает naive datetime). Схема ведётся Alembic-миграциями
(`db/migrations/`, baseline автогенерён из ORM-метаданных): на старте composition root
вызывает `bootstrap_schema` — свежая или уже-управляемая БД мигрируется до head; БД, созданная
до Alembic (таблицы есть, `alembic_version` нет), штампуется на baseline и догоняется до head
(ALTER-миграции добавляют колонки условно — legacy-БД могла быть создана более новым
`create_all`). `init_db` (`create_all`)
остаётся для тестов и как fallback в composition root, если миграции не удалось применить.

## API (`octoforge_web/api/`)

Реализовано (диалог — get-or-create по (user_id, channel); канал `"web"` объявлен в composition root):

- все эндпоинты диалога требуют заголовок `X-User-Id` (доверенная строка до появления
  аутентификации); отсутствующий/пустой → 400
- `POST /api/dialog/messages` `{content}` → 202 `{status: "accepted"}` — сообщение уходит
  роутеру: новый процесс, инъекция в форграунд, отмена или promote — по его решению
- `POST /api/dialog/cancel` → 202 — мягкая отмена форграунд-процесса (явная просьба)
- `GET /api/dialog/events` — SSE-подписка на события диалога (`iteration_started`,
  `text_delta`, `assistant_message`, `tool_call_*`, `finished`, `cancelled`, `failed`,
  маркеры процессов `process_suspended`/`process_resumed`/`process_completed`;
  heartbeat-комментарии; в кадрах `seq` и `dialog_id`); диалог создаётся при первом обращении,
  поэтому подписаться можно до первого сообщения
- `GET /health` — liveness (`{status: ok}`); `GET /health/ready` — readiness: проверяет
  `SELECT 1` к БД, при недоступности возвращает 503 `{status: not-ready}`
- `GET /` — чат-UI (SSE-стрим токенов, шаги скилов, серая курсивная
  строка-маркер о переключениях/завершениях процессов, «Стоп», поле имени =
  user_id, уходит заголовком `X-User-Id`; EventSource не умеет кастомные заголовки, поэтому
  стрим читается через fetch)
- крон-задачи (`/api/cron`, скоуп по `X-User-Id`, параметры — query string):
  `POST /api/cron/jobs?title=&schedule=&prompt=&timezone=` → 201 + JSON задачи
  (невалидные cron-выражение/таймзона → 422; `next_fire_at` вычисляется от now);
  `GET /api/cron/jobs` → список задач юзера; `DELETE /api/cron/jobs/{id}` → 204
  (чужая/несуществующая → 404); `POST /api/cron/jobs/{id}/pause` → enabled=false;
  `POST /api/cron/jobs/{id}/resume` → enabled=true с `next_fire_at` от now

План:

- аутентификация: `users`, токены (`POST /api/users` с admin-secret, `Authorization: Bearer`),
  служебные токены для wake/cron
- `GET /api/skills`, `GET /api/tasks`
- `GET /ws` — при необходимости двунаправленного канала

## Тесты (pytest + pytest-asyncio)

Реализовано:

- `core/tests/test_openai_stream.py` — SSE-парсинг (дельты, склейка tool_calls, [DONE]), `aclose`
- `core/tests/test_openai_client.py` — non-streaming вызов, tools, tool-история, ошибки
- `core/tests/test_agent_loop.py` — события прогона, инъекция mid-run, отмена с частичным текстом, ошибка скила, лимит итераций
- `core/tests/test_conversation_runner.py` — процессы: submit → fg-стрим + ProcessCompleted;
  новый вопрос mid-run → ProcessSuspended + новый fg (bg дорабатывает молча); INJECT-руление;
  PROMOTE → ProcessResumed; CANCEL по решению роутера; cancel() снимает только форграунд;
  лимит (max=1) → отказ-заметка инъекцией (fg занят) и репорт-прогоном (fg свободен);
  task_spawn через скил → bg-процесс → уведомление + репорт + `result_delivered`;
  уведомление инъекцией в занятый fg; гигиена прерванного тёрна; недочитанная инъекция
  получает собственный процесс; пересборка нарратива после «перезапуска» менеджера;
  wake крон-задачи → bg-процесс с `cron_job_id` в `Task.input`, лимит → системная заметка
  (SQLite :memory:; роутер — детерминированный fake, не LLM)
- `core/tests/test_router.py` — LLMRouter на mock LLMClient: passthrough без вызова при
  пустом снимке, парсинг пакетов ops, отбрасывание невалидных (action, лишний/чужой/пустой
  target), фолбэки (нет tool_call, ошибка, таймаут), форма запроса (процессы, лимит, tool spec)
- `core/tests/test_db_repositories.py` — диалоги (get-or-create, уникальность пары), сообщения
  (seq/порядок, tool_calls round-trip, изоляция), UTCDateTime round-trip, SqlAlchemyTaskStore
  (те же сценарии, что у InMemoryTaskStore, + cancel/is_cancelled/count_active, mark_delivered)
- `core/tests/test_tasks.py` — task_spawn через fake-спавнер (делегация, отказ текстом,
  отсутствие спавнера — SkillArgumentsError), валидация, task_list по диалогу со статусом
  cancelled, InMemoryTaskStore: cancel/is_cancelled/count_active, mark_delivered, UTC-даты
- `core/tests/test_http_request_skill.py`, `core/tests/test_skills_registry.py`
- `core/tests/test_instructions_local.py` — контрактный набор фасада на `:memory:` (save/upsert
  с бампом версии и пересчётом эмбеддинга, get_by_name с сужением по типу, ранжирование:
  ближний вектор, буст точного title, k, инкремент usage_count, сидирование один раз;
  сид крон-тулов: записи указывают на self base URL, идемпотентность, независимость от
  weather-маркера); сервис строится фабрикой-фикстурой — тот же набор позже прогонит
  http-реализацию
- `core/tests/test_embeddings.py` — клиент эмбеддингов поверх мокнутого httpx-транспорта
  (форма запроса, разбор ответа с переупорядочиванием по index, ошибки статуса и payload)
- `core/tests/test_external_call.py` — парсинг/валидация ToolSpec, рендер шаблона с quoting,
  валидация параметров, whitelist-заголовок только для своего префикса, блок SSRF,
  редирект не следуется, обрезка тела; темплейт `{user_id}` в значении заголовка
  (подстановка, отсутствие user_id → без заголовка, статичное значение неизменно),
  allowlist-префикс пропускает loopback, скил передаёт `context.user_id`
- `core/tests/test_ssrf_guard.py` — stub-resolver: private/loopback/link-local/multicast/CGNAT/
  IPv6 заблокированы, публичные разрешены, любой приватный из многих блокирует, неразрешимый
  хост и не-http(s) URL заблокированы; allowed_prefixes — пропуск без resolve, остальные
  URL проверяются по-прежнему
- `core/tests/test_cron_store.py` — SQL-стор крона на `:memory:`: CRUD, изоляция юзеров
  (delete/set_enabled чужого → CronJobNotFoundError), list_due (enabled, окно, свежая и
  протухшая аренда, порядок, лимит), claim CAS (гонка, сдвинутый next_fire_at, перехват
  протухшей аренды), release_claim, complete_fire, record_fire_result (retry_at →
  сдвиг next_fire_at + инкремент серии, без ретрая → сброс)
- `core/tests/test_cron_scheduler.py` — реальный стор + записывающий fake-waker: выстрел
  due с пересчётом next_fire_at в будущее, будущая/disabled не стреляют, coalesce
  пропущенных с суффиксом, лимит реплея (остаток ждёт следующий тик), failed wake →
  release_claim, чужая свежая аренда → пропуск; compute_next_fire по таймзоне
  (Europe/Moscow 9:00 = 06:00 UTC), невалидные schedule/timezone → CronScheduleError,
  count_missed (минус текущий выстрел, cap); порт Scheduler (соответствие и подмена)
- `core/tests/test_cron_reporter.py` — политика исходов: DONE сбрасывает серию ретраев,
  DONE удаляет one-shot задачу, FAILED → backoff с инкрементом серии, лимит → без ретрая
  + сброс, CANCELLED без ретрая, удалённая/непомеченная задача — тихо, обрезка ошибки
  до 500
- `core/tests/test_cron_skills.py` — скилы cron_*: create (one_shot-флаг, дедуп тройки
  → «already exists»), list с `last run`/`retry #N`, delete/pause/resume, owner-изоляция
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
- `core/tests/test_instructions_store_port.py`, `core/tests/test_datasets_store_port.py` —
  подмена store-портов (P1 модульности): in-memory `InstructionStore`/`DatasetStore`
  инъектируются в немодифицированные сервисы (save/search/get/delete без SQL), vector-capable
  fake получает `search_by_vector` (делегирование, owner в сигнатуре, буст поверх кандидатов)
- `core/tests/test_composition.py` — переиспользуемые builder'ы (P5 модульности): полный
  набор базовых скилов из `build_skill_registry` (без `web_search` при `search_provider=None`),
  подмена портов через builder'ы (fake `SearchProvider`, in-memory `InstructionStore`),
  рабочий `build_conversation_manager` на SQLite `:memory:` с прогоном диалога
- `core/tests/test_prompts.py` — `StaticPromptProvider`: вшитые дефолты, кастомный маппинг,
  KeyError на неизвестное имя; `test_router.py` += роутерный промпт из провайдера
- `core/tests/test_web_search_skill.py` — скил над fake-`SearchProvider` (подмена P3):
  форматирование answer box и позиций, клэмп num_results до провайдера, «no results»,
  SearchError → текст ошибки, срез длинного вывода
- `core/tests/test_serper_provider.py` — `SerperSearchProvider` на мокнутом httpx: заголовок
  ключа и тело запроса, парсинг answerBox/organic (cap по num), HTTP-ошибка и сетевой сбой
  → `SearchError`
- `web/tests/test_config.py` — Settings: дефолты OF_EMBEDDING_*/top-k/whitelist/лимитов
  data_query и memory_search, max_processes и router_timeout_seconds (дефолты и env),
  self_base_url и OF_CRON_* (дефолты и env), парсинг JSON-whitelist и лимитов из env,
  OF_*_PROMPT_SOURCE (file:-источники, неизвестная схема → ValueError)
- `web/tests/test_prompts.py` — `FilePromptProvider`: чтение из файла, перечитывание на
  каждый get(), fallback на StaticPromptProvider (нет файла/файл нечитаем + warning),
  KeyError на неизвестное имя
- `web/tests/test_modularity.py` — приёмочный сценарий модульности: минимальный сторонний
  composition root (без `main.runtime()`), собранный из core-builder'ов
  (`octoforge_core.composition`): системный+роутерный промпты из файлов,
  fake-`SearchProvider`, in-memory `InstructionStore` — диалог прогоняется целиком
  (промпты доезжают до LLM, скилы выполняются над подменёнными компонентами)
- `web/tests/test_dialog_api.py` — get-or-create диалога, изоляция двух user_id, 400 без
  `X-User-Id`, messages/cancel/events (SSE через генератор), health, UI
- `web/tests/test_cron_api.py` — create (201 + поля, next_fire_at в будущем, default UTC),
  невалидные schedule/timezone/пропущенный параметр → 422, list с изоляцией двух юзеров,
  delete 204/404 (чужая и несуществующая), pause/resume (resume пересчитывает
  next_fire_at; чужие → 404), 400 без `X-User-Id` на всех эндпоинтах
- `web/tests/test_sse.py` — сериализация событий в SSE-кадры (включая маркеры процессов)
- `web/tests/test_telegram_models.py` — парсинг update'ов Bot API (лишние поля игнорируются,
  алиас `from`, неизвестный тип чата отклоняется)
- `web/tests/test_telegram_bridge.py` — мост на реальном manager'е (ScriptedLLM) + fake
  TelegramClient: дельты → одно редактируемое сообщение, статус-строки скилов перед ответом,
  чанкинг >4096 (seal и продолжение), отмена и ошибка LLM — строками в черновике; чанкер
  `split_message` (границы строки/слова, жёсткий рез)
- `web/tests/test_telegram_poller.py` — команды `/start`/`/cancel`, уведомления группе и
  не-тексту, текст → диалог → ответ, offset-продвижение и backlog-drain при старте,
  восстановление после ошибок поллинга, `chat_id_from_user_id`, прогрев мостов (только
  tg-префиксованные user_id)

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
8. ✅ LLM-роутер и процессная модель диалога (этап E) — см. [process-model.md](process-model.md)
9. Инструкции в БД (знание/скил/тул + векторный поиск, этап B ✅) — см. [instructions.md](instructions.md); датасеты пользовательских данных (этап C ✅) — см. [data-store.md](data-store.md); крон-задачи (этап F ✅) — см. [cron.md](cron.md)
10. Бэклог из обзора openclaw (SSRF-гвард, формула поиска, каталог скилов, детали крона и пр.) — см. [openclaw-review.md](openclaw-review.md)
11. ✅ Telegram-адаптер (этап G): вторая поверхность (канал "telegram", user_id = tg:<id>), long-poll на httpx, мосты с throttle-правками, прогрев из БД — см. «Telegram-адаптер (этап G)» выше

## Проверка

- `make check` (ruff → mypy → pytest) — всё зелёное
- Ручной сценарий: `make run` → http://127.0.0.1:8000 → токены текут по мере генерации; «выполни GET к <url>» — шаг скилла виден в чате; «реши в фоне X» — агент подтверждает и продолжает диалог, результат приходит сам; «Стоп» — ответ обрывается; два разных имени — истории и задачи изолированы; перезапуск приложения не теряет диалог (история восстанавливается из БД)
- Целевой сценарий: два юзера → память не смешивается, скил общий, задачи разных юзеров изолированы
