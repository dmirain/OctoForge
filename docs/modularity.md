# Модульность и подменяемость ядра (аудит + дорожная карта)

> **Статус: аналитический документ + roadmap (код не менялся).** Цель — чтобы тот, кто
> ставит OctoForge в свою экосистему, мог подменить ключевые компоненты **без переписывания
> ядра**: поиск/выдачу инструкций, cron, промпты ядра, интеграцию с веб-поиском, память и
> другие выделенные швы. Связанные доки: [design.md](design.md), [cron.md](cron.md),
> [instructions.md](instructions.md), [data-store.md](data-store.md).

## Рамка и принятые решения

Проект построен по гексагональной схеме: порты-`Protocol` + адаптеры, ручной DI через
конструкторы, единый composition root `web/src/octoforge_web/main.py:runtime()`. Ядро
`octoforge-core` не импортирует конкретные адаптеры — только порты. Это хороший фундамент;
пробелы — там, где компонент не имеет порта и жёстко создаётся внутри ядра.

- **Механизм расширения:** порты + собственный composition root. Инсталлятор пишет *свой*
  корень сборки, переиспользуя `octoforge-core` как библиотеку; ядро не редактируется.
  Главное — достроить недостающие порты и сделать сборку переиспользуемой (не plugin-discovery
  через entry-points).
- **Промпты:** переопределяются из внешнего источника (файл/конфиг) через `PromptProvider`;
  роутерный промпт выносится за пределы `LLMRouter`.
- Каждый этап реализации = порт + дефолтная реализация + перенос wiring в корень + тесты +
  запись в [design.md](design.md); `make check` зелёный в конце.

## Текущее состояние: что уже подменяемо чисто

Эти швы уже DI-подменяемы из composition root — эталон, трогать не нужно:

| Компонент | Порт | Файл порта |
|---|---|---|
| LLM-клиент | `LLMClient` | `core/.../ports.py:12` |
| Роутер сообщений | `MessageRouter` | `core/.../agent/router.py:53` |
| Память (key/value) | `MemoryStore` (порт *есть* граница хранилища) | `core/.../memory/api.py:47` |
| Cron: хранилище | `CronStore` | `core/.../cron/api.py:57` |
| Cron: доставка задачи | `CronWaker` | `core/.../cron/api.py:128` |
| Эмбеддинги | `EmbeddingClient` | `core/.../llm/embeddings.py:18` |
| Реранкер | `RerankerClient` | `core/.../llm/reranker.py:22` |
| Instruction/Dataset (фасад) | `InstructionService`, `DatasetService` | `.../instructions/api.py:61`, `.../datasets/api.py:107` |
| Скиллы | `Skill` + `SkillRegistry` | `core/.../skills/base.py:41` |
| Задачи (bg) | `TaskStore` | `core/.../ports.py:32` |

Память — эталонный модуль: порт *и есть* граница хранилища, подмена = одна строка в корне.

## Пробелы и целевые изменения (по приоритету)

### P1 — Порт хранилища для instructions и datasets (главный пробел)

**Проблема.** Есть чистый `InstructionService`/`DatasetService`-порт, но SQL-хранилище жёстко
создаётся *внутри* сервиса:
- `LocalInstructionService.__init__` → `self._store = InstructionStore(session_factory)`
  (`core/.../instructions/local.py:36`)
- `LocalDatasetService` → `DatasetStore(...)` (`core/.../datasets/service.py:35`)

Нет порта уровня хранилища. Чтобы поставить pgvector / внешний вектор-BD, инсталлятор вынужден
переписать **весь `*Service`** (оркестрацию ранжирования + эмбеддинги), а не подменить только
слой хранения. Плюс `store.list_with_embeddings()` возвращает всю таблицу для brute-force cosine
в Python (`instructions/store.py:69`, `datasets/store.py:117`) — контракт «маленькие данные,
ранжируем в процессе», несовместимый с векторным поиском на стороне БД.

**Целевое решение.** Ввести Protocol-порты хранилища и инъектировать их в сервис:
- `InstructionStore` (Protocol) в `instructions/api.py`; текущий SQL-класс переименовать в
  `SqlInstructionStore` и объявить реализацией.
- `DatasetStore` (Protocol) в `datasets/api.py`; аналогично.
- `LocalInstructionService`/`LocalDatasetService` принимают `store` через конструктор.
- Развилка «ранжируем в Python vs делегируем поиск в БД»: расширить порт хранилища опциональным
  методом семантического поиска (например `search_by_vector(query_embedding, k)`), чтобы
  pgvector-реализация не тянула всю таблицу. `ranking.py` остаётся чистым и используется только
  in-process-реализацией.

**Файлы:** `instructions/api.py`, `instructions/local.py`, `instructions/store.py`,
`datasets/api.py`, `datasets/service.py`, `datasets/store.py`, wiring в `main.py:124,130`.

### P2 — `PromptProvider`: промпты из внешнего источника

**Проблема.** `DEFAULT_SYSTEM_PROMPT` (`core/.../agent/prompts.py:3`) и `ROUTER_SYSTEM_PROMPT`
(`core/.../agent/router.py:96`) — хардкод-константы. Системный хотя бы инъектится как
`RunnerConfig.system_prompt` (`runner.py:119`, wiring `main.py:162`), а роутерный вшит в
`LLMRouter._build_messages` (`router.py:167`) и не переопределяется без правки ядра.

**Целевое решение.**
- Ввести порт `PromptProvider` (Protocol, метод вида `get(name) -> str`) и дефолтную реализацию
  `StaticPromptProvider` поверх вшитых констант (fallback).
- Файловая/конфиг-реализация в `web/` читает источник из `OF_SYSTEM_PROMPT_SOURCE` /
  `OF_ROUTER_PROMPT_SOURCE` (напр. `file:/path` со fallback на вшитый дефолт). Ядро остаётся без
  чтения env.
- Вынести роутерный промпт из `LLMRouter`: принимать его (или `PromptProvider`) через конструктор.
- Системный промпт: `RunnerConfig.system_prompt` заменить на источник из `PromptProvider`
  (сохранив текущую подстановку даты `_with_current_date`, `runner.py`).

**Файлы:** `core/.../agent/prompts.py` (порт + `StaticPromptProvider`), `agent/router.py`
(конструктор), `agent/runner.py` (`RunnerConfig`), новая web-реализация, wiring `main.py:162-166`,
`web/config.py` (новые `OF_*`).

### P3 — `SearchProvider`-порт для веб-поиска

**Проблема.** `WebSearchSkill` (`core/.../skills/basic/web_search.py:34`) захардкожен на serper.dev:
URL (`web_search.py:9`), заголовок `X-API-KEY` (`:10`), парсинг ответа serper (`_format_results`).
Порта провайдера нет — чтобы поменять serper → Bing/Brave/Tavily, надо править ядро.

**Целевое решение.** Ввести порт `SearchProvider` (Protocol): `search(query) -> list[SearchResult]`
(транспорт-нейтральные DTO). `WebSearchSkill` зависит от порта, а не от serper.
`SerperSearchProvider` — дефолтная реализация. Инсталлятор регистрирует скилл со своим провайдером.

**Файлы:** новый порт (`core/.../search/api.py` или рядом со скиллом), `skills/basic/web_search.py`,
`SerperSearchProvider`, wiring `main.py:143-147`.

### P4 — `Scheduler`-порт для cron-движка

**Проблема.** Хранилище и waker cron за портами, но сам движок `CronScheduler` (asyncio-поллинг +
CAS-lease) — конкретный класс без порта, стартуется напрямую в `_start_cron_scheduler`
(`main.py:272-282`). Формального шва «подключить свой движок» (Celery beat / APScheduler / OS cron)
нет.

**Целевое решение.** Ввести минимальный порт `Scheduler` (Protocol: `run_forever()` / lifecycle);
`CronScheduler` объявить реализацией. Примитивы для внешних движков уже публичны и
транспорт-нейтральны: `CronStore.list_due/claim/complete_fire` + `compute_next_fire`/`count_missed`
(`cron/api.py:143-178`) — задокументировать как публичный контракт для альтернативных движков.
Инсталлятор либо подменяет `Scheduler`, либо не стартует наш и вызывает store + schedule-math из
своего движка.

**Файлы:** `cron/api.py` (порт), `cron/scheduler.py` (nominal), `main.py:272`, [cron.md](cron.md).

### P5 — Переиспользуемый composition root

**Проблема.** `runtime()` (`main.py:98-187`) — монолитная сборка ~90 строк. При подходе «свой
корень» инсталлятор вынужден копировать её целиком ради подмены одного компонента.

**Целевое решение.** Разложить сборку на переиспользуемые builder-функции с параметрами-портами:
`build_skill_registry(...)`, `build_conversation_manager(...)`, `build_instruction_service(...)`
и т.п. Существующие `_register_*`-хелперы (`main.py:335-409`) поднять в переиспользуемый слой.
`runtime()` остаётся дефолтной сборкой поверх builder'ов; сторонний корень собирает своё из тех же
кирпичей. Слой без зависимости от FastAPI — в `octoforge-core`; web-специфика (Telegram/HTTP) — в
`web/`. Публичный API ядра (`octoforge_core/__init__.py`) экспортирует все порты и builder'ы.

**Файлы:** `main.py`, возможно новый `core/.../composition.py` или `web/.../assembly.py`,
`octoforge_core/__init__.py`.

### P6 — Косметика: конкретные аннотации в корне

`main.py:359` и `main.py:378` (`_register_instruction_skills`, `_register_dataset_skills`)
типизируют параметр как конкретный `LocalDatasetService` вместо порта `DatasetService`. Заменить на
порт для консистентности (память/instructions-хелперы уже используют порты); ломает type-check при
подмене `DatasetService` на HTTP-клиент.

## Дополнительно (низкий приоритет)

- **`TaskSpawner`** (`tasks/spawner.py:6`) и **`HostResolver`** (`net/guard.py:28`) — реализации
  фиксируются *внутри* ядра (`runner.py:145`, `guard.py:56`), корень их не инъектирует. `SsrfGuard`
  уже принимает `resolver=`, но корень не передаёт. Пробросить в корень для полноты DI, если нужна
  подмена DNS-резолвинга / спавна задач.
- **`EmbeddingBackend`-enum** (`core/config.py:10`): новый бэкенд эмбеддингов требует правки enum +
  ветки в `_build_embedder`. При «своём корне» не блокер (инсталлятор передаёт свой `EmbeddingClient`
  напрямую), но enum-развилка — легаси config-driven-подхода.

## Приоритетный порядок работ

1. **P1 — Store-порты instructions/datasets** — главный пробел; разблокирует pgvector/внешний вектор-BD.
2. **P2 — PromptProvider** — изолированный, невысокий риск.
3. **P3 — SearchProvider** — маленький, изолированный.
4. **P4 — Scheduler-порт** — в основном формализация + docs.
5. **P5 — Декомпозиция composition root** — после P1–P4, когда набор портов стабилизируется.
6. **P6 — Косметика аннотаций** — тривиально, заодно с P1.

## Верификация (для этапа реализации)

Инвариант каждого шага — **ядро не импортирует конкретный адаптер, только порт**, подмена делается
из корня без правки `core/`.

- `make check` в обоих проектах (ruff + `mypy --strict` + pytest).
- На каждый новый порт — тест с *альтернативной* реализацией (fake/in-memory), подставленной вместо
  дефолтной, доказывающий подмену без правки ядра.
- Единичные тесты из нужного проекта: `cd core && ../.venv/bin/pytest ...`,
  `cd web && ../.venv/bin/pytest ...`.
- Приёмочный сценарий модульности: собрать *минимальный сторонний composition root* в тесте, который
  переопределяет системный+роутерный промпт из файла, подставляет fake-`SearchProvider` и
  in-memory `InstructionStore`, и прогнать через него диалог.
- E2E-дым: `make run` (chat UI на http://127.0.0.1:8000) и `make run-telegram` — дефолтная сборка не
  сломана.
