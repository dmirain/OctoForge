# Модульность и подменяемость ядра (аудит + дорожная карта)

> **Статус: P1–P4 и P6 реализованы; P5 (переиспользуемый composition root) впереди.**
> Цель — чтобы тот, кто ставит OctoForge в свою экосистему, мог подменить ключевые
> компоненты **без переписывания ядра**: поиск/выдачу инструкций, cron, промпты ядра,
> интеграцию с веб-поиском, память и другие выделенные швы. Связанные доки:
> [design.md](design.md), [cron.md](cron.md), [instructions.md](instructions.md),
> [data-store.md](data-store.md).

## Рамка и принятые решения

Проект построен по гексагональной схеме: порты-`Protocol` + адаптеры, ручной DI через
конструкторы, единый composition root `web/src/octoforge_web/main.py:runtime()`. Ядро
`octoforge-core` не импортирует конкретные адаптеры — только порты.

- **Механизм расширения:** порты + собственный composition root. Инсталлятор пишет *свой*
  корень сборки, переиспользуя `octoforge-core` как библиотеку; ядро не редактируется.
  Набор портов достроен (P1–P4 ниже); осталась декомпозиция самого корня (P5).
- **Промпты:** переопределяются из внешнего источника (файл/конфиг) через `PromptProvider`;
  роутерный промпт вынесен за пределы `LLMRouter` (P2 сделано).
- Каждый этап реализации = порт + дефолтная реализация + перенос wiring в корень + тесты +
  запись в [design.md](design.md); `make check` зелёный в конце.

## Текущее состояние: что подменяемо чисто

Эти швы DI-подменяемы из composition root без правки `core/`:

| Компонент | Порт | Файл порта |
|---|---|---|
| LLM-клиент | `LLMClient` | `core/.../ports.py` |
| Роутер сообщений | `MessageRouter` | `core/.../agent/router.py` |
| Память (key/value) | `MemoryStore` | `core/.../memory/api.py` |
| Cron: хранилище | `CronStore` | `core/.../cron/api.py` |
| Cron: доставка задачи | `CronWaker` | `core/.../cron/api.py` |
| Cron: движок планирования | `Scheduler` | `core/.../cron/api.py` |
| Эмбеддинги | `EmbeddingClient` | `core/.../llm/embeddings.py` |
| Реранкер | `RerankerClient` | `core/.../llm/reranker.py` |
| Instruction/Dataset (фасад) | `InstructionService`, `DatasetService` | `.../instructions/api.py`, `.../datasets/api.py` |
| Instructions/Datasets: хранилище | `InstructionStore`, `DatasetStore` (+`*VectorSearch`) | `.../instructions/api.py`, `.../datasets/api.py` |
| Промпты ядра | `PromptProvider` | `core/.../agent/prompts.py` |
| Веб-поиск | `SearchProvider` | `core/.../search/api.py` |
| Скиллы | `Skill` + `SkillRegistry` | `core/.../skills/base.py` |
| Задачи (bg) | `TaskStore` | `core/.../ports.py` |

Память — эталонный модуль: порт *и есть* граница хранилища, подмена = одна строка в корне.
Store-порты instructions/datasets повторяют этот паттерн (P1).

## Реализованные этапы

### P1 — Порт хранилища для instructions и datasets ✅

**Что было.** Чистый `InstructionService`/`DatasetService`-порт, но SQL-хранилище жёстко
создавалось *внутри* сервиса; подмена слоя хранения (pgvector, внешний вектор-BD) требовала
переписать весь сервис, а `list_with_embeddings()` тянул всю таблицу для brute-force cosine.

**Что сделано.**
- `InstructionStore` (Protocol) в `instructions/api.py`; SQL-класс переименован в
  `SqlAlchemyInstructionStore` (по конвенции `SqlAlchemyMemoryStore`/`SqlAlchemyCronStore`,
  а не `SqlInstructionStore` из исходного плана). `EmbeddedInstruction` переехал из
  `ranking.py` в `api.py` (он — часть контракта порта).
- `DatasetStore` (Protocol) в `datasets/api.py`; SQL-класс — `SqlAlchemyDatasetStore`;
  `EmbeddedDataset` — тоже в `api.py`; `MAX_SCAN_ROWS` переехал в `service.py`
  (это политика сервиса, не хранилища).
- `LocalInstructionService`/`LocalDatasetService` принимают `store` конструктором.
- Развилка «ранжируем в Python vs делегируем поиск в БД»: runtime-checkable
  capability-порты `InstructionVectorSearch`/`DatasetVectorSearch`
  (`search_by_vector(...) -> list[Embedded*]`). Сервис проверяет `isinstance` и либо
  делегирует выбор кандидатов стору, либо тянет `list_with_embeddings()`; буст точного
  title/name и реранк остаются на сервисе. `ranking.py` чист и используется только
  in-process-путём.
- Тесты подмены: `core/tests/test_instructions_store_port.py`,
  `core/tests/test_datasets_store_port.py` (in-memory сторы, vector-capable fake с проверкой
  делегирования и отсутствия полного скана).

### P2 — `PromptProvider`: промпты из внешнего источника ✅

**Что было.** `DEFAULT_SYSTEM_PROMPT` и `ROUTER_SYSTEM_PROMPT` — хардкод-константы;
роутерный был вшит в `LLMRouter._build_messages` и не переопределялся без правки ядра.

**Что сделано.**
- Порт `PromptProvider` (`get(name) -> str`) и `StaticPromptProvider` поверх вшитых
  констант — в `agent/prompts.py`; имена промптов — `SYSTEM_PROMPT_NAME`/
  `ROUTER_PROMPT_NAME`; `ROUTER_SYSTEM_PROMPT` переехал из `router.py` в `prompts.py`.
- `LLMRouter` принимает провайдер конструктором и форматирует шаблон
  (`{limit}`/`{processes}`) на каждый вызов; `RunnerConfig.system_prompt: str` заменён на
  `prompts: PromptProvider` — раннер берёт системный промпт из провайдера при старте
  процесса, суффикс текущей даты (`_with_current_date`) сохранён.
- Файловая реализация на web-слое: `FilePromptProvider` (`web/.../prompts.py`) читает
  источники `OF_SYSTEM_PROMPT_SOURCE`/`OF_ROUTER_PROMPT_SOURCE` (формат `file:/path`,
  парсинг — `Settings.to_prompt_files()`, неизвестная схема → ValueError при старте).
  Файл перечитывается на каждый `get()` (правка без рестарта); нечитаемый файл или
  ненастроенное имя → fallback на `StaticPromptProvider` (warning в лог). Ядро env не
  читает.
- Тесты: `core/tests/test_prompts.py`, `web/tests/test_prompts.py`, `test_router.py` +=
  кастомный промпт, `test_config.py` += парсинг источников.

### P3 — `SearchProvider`-порт для веб-поиска ✅

**Что было.** `WebSearchSkill` захардкожен на serper.dev (URL, заголовок, парсинг ответа);
замена поисковика требовала правки ядра.

**Что сделано.**
- Новый модуль `search/`: порт `SearchProvider` (`search(query, num_results)`), DTO
  `SearchResult`/`SearchResponse`, ошибка `SearchError` в `search/api.py`. Отступление от
  исходного плана (`-> list[SearchResult]`): ответ — `SearchResponse(results, answer)`,
  чтобы сохранить answer box serper в транспорт-нейтральном виде.
- `SerperSearchProvider` (`search/serper.py`) — дефолтная реализация: запрос, парсинг
  answerBox/organic, тексты ошибок прежние («search failed: ConnectError», «search API
  returned HTTP N»).
- `WebSearchSkill(provider=...)` зависит от порта; форматирование выдачи, клэмп
  `num_results` (1..10) и срез вывода остаются на скилле. Wiring: `main.py` регистрирует
  скилл с serper-провайдером при `OF_SERPER_TOKEN`.
- Тесты: `test_web_search_skill.py` переписан на fake-провайдер (он же — доказательство
  подмены), `test_serper_provider.py` — HTTP-моки serper.

### P4 — `Scheduler`-порт для cron-движка ✅

**Что было.** Хранилище и waker cron за портами, но движок `CronScheduler` — конкретный
класс без порта; формального шва «подключить свой движок» не было.

**Что сделано.**
- Порт `Scheduler` (Protocol, `run_forever()`; runtime_checkable) в `cron/api.py`;
  `CronScheduler` — реализация по умолчанию, wiring в `main.py` типизирован портом.
- `CronStore.list_due`/`claim`/`release_claim`/`complete_fire` + `compute_next_fire`/
  `count_missed` задокументированы как публичный контракт для альтернативных движков
  (docstring `cron/api.py` и [cron.md](cron.md)): инсталлятор либо подменяет `Scheduler`,
  либо не стартует наш и драйвит store + математику из своего движка (Celery beat,
  APScheduler, OS cron).
- Тесты: `test_cron_scheduler.py` += соответствие порту и подмена альтернативным движком.

### P6 — Косметика: конкретные аннотации в корне ✅

`_register_instruction_skills`/`_register_dataset_skills` в `main.py` типизируют параметр
портом `DatasetService` (было `LocalDatasetService`) — сделано заодно с P1, type-check при
подмене `DatasetService` на HTTP-клиент больше не ломается.

## Оставшиеся этапы

### P5 — Переиспользуемый composition root

**Проблема.** `runtime()` (`main.py`) — монолитная сборка ~90 строк. При подходе «свой
корень» инсталлятор вынужден копировать её целиком ради подмены одного компонента.

**Целевое решение.** Разложить сборку на переиспользуемые builder-функции с параметрами-
портами: `build_skill_registry(...)`, `build_conversation_manager(...)`,
`build_instruction_service(...)` и т.п. Существующие `_register_*`-хелперы поднять в
переиспользуемый слой. `runtime()` остаётся дефолтной сборкой поверх builder'ов; сторонний
корень собирает своё из тех же кирпичей. Слой без зависимости от FastAPI — в
`octoforge-core`; web-специфика (Telegram/HTTP) — в `web/`. Публичный API ядра
(`octoforge_core/__init__.py`) экспортирует все порты и builder'ы.

**Файлы:** `main.py`, возможно новый `core/.../composition.py` или `web/.../assembly.py`,
`octoforge_core/__init__.py`.

## Дополнительно (низкий приоритет)

- **`TaskSpawner`** (`tasks/spawner.py`) и **`HostResolver`** (`net/guard.py`) — реализации
  фиксируются *внутри* ядра (`runner.py`, `guard.py`), корень их не инъектирует. `SsrfGuard`
  уже принимает `resolver=`, но корень не передаёт. Пробросить в корень для полноты DI,
  если нужна подмена DNS-резолвинга / спавна задач.
- **`EmbeddingBackend`-enum** (`core/config.py`): новый бэкенд эмбеддингов требует правки
  enum + ветки в `_build_embedder`. При «своём корне» не блокер (инсталлятор передаёт свой
  `EmbeddingClient` напрямую), но enum-развилка — легаси config-driven-подхода.

## Приоритетный порядок работ

1. ✅ **P1 — Store-порты instructions/datasets.**
2. ✅ **P2 — PromptProvider.**
3. ✅ **P3 — SearchProvider.**
4. ✅ **P4 — Scheduler-порт.**
5. **P5 — Декомпозиция composition root** — следующий шаг (набор портов стабилизировался).
6. ✅ **P6 — Косметика аннотаций** (сделано заодно с P1).

## Верификация

Инвариант каждого шага — **ядро не импортирует конкретный адаптер, только порт**, подмена
делается из корня без правки `core/`.

- `make check` в обоих проектах (ruff + `mypy --strict` + pytest) — зелёный.
- На каждый новый порт — тест с *альтернативной* реализацией (fake/in-memory),
  подставленной вместо дефолтной: `test_instructions_store_port.py`,
  `test_datasets_store_port.py`, `test_web_search_skill.py` (fake-провайдер),
  `test_router.py` + `test_prompts.py` (кастомный провайдер промптов),
  `test_cron_scheduler.py` (альтернативный движок).
- Единичные тесты из нужного проекта: `cd core && ../.venv/bin/pytest ...`,
  `cd web && ../.venv/bin/pytest ...`.
- Приёмочный сценарий модульности — реализован: `web/tests/test_modularity.py` собирает
  *минимальный сторонний composition root*, переопределяющий системный+роутерный промпт из
  файла, с fake-`SearchProvider` и in-memory `InstructionStore`, и прогоняет через него
  диалог (промпты доезжают до LLM, скилы выполняются над подменёнными компонентами).
- E2E-дым: `make run` (chat UI на http://127.0.0.1:8000) и `make run-telegram` — дефолтная
  сборка не сломана.
