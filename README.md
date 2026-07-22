# OctoForge

Мультипользовательский LLM-агент с саморасширяемыми знаниями: скилы и знания хранятся
в БД, агент умеет уводить работу в фон и по расписанию, а ядро оформлено как библиотека,
которую можно встроить куда угодно.

## Мотивация

Обычно агент — это код с зашитыми инструментами: новая возможность = новый деплой.
OctoForge строится вокруг другой идеи:

- **Агент растёт в рантайме.** Знания, скилы, HTTP-тулы, датасеты и память живут
  в БД (SQLite, async SQLAlchemy) и ищутся по эмбеддингам.
  Агент сам сохраняет новые инструкции и записи — без изменения кода.
- **Длинная работа не блокирует диалог.** Процессная модель: задачи уходят в фон
  с уведомлением о результате, периодические задания выполняет встроенный
  крон-планировщик, входящие сообщения разруливает LLM-роутер (инъекция в текущий
  прогон / новый процесс / отмена).
- **Одно ядро — много поверхностей.** Веб-чат и Telegram-бот работают на одном
  движке диалогов; ядро (`octoforge-core`) не зависит от FastAPI и встраивается
  как обычная Python-библиотека.

По духу это аналог openclaw, но со знаниями в БД вместо файлов.

## Архитектура

Монорепо из двух независимых Python-проектов (у каждого свой `pyproject.toml`,
зависимости и тесты):

- `core/` — библиотека `octoforge-core`: домен, порты-Protocol, агентная петля,
  процессная модель, скилы, модули instructions/datasets/memory/cron, клиенты LLM
  и эмбеддингов, персист. **Не импортирует FastAPI.**
- `web/` — приложение `octoforge-web`: тонкий адаптер — FastAPI-обёртка (REST + SSE),
  чат-UI, Telegram-адаптер, composition root (`web/src/octoforge_web/main.py`,
  функция `runtime()` — общая для web и standalone-режимов).

Поток сообщения:

```
 Поверхности     web UI + HTTP API (FastAPI)       Telegram-бот (long poll)
                        \                             /
                         ConversationManager — runner на (user_id, channel)
                                         |
                           ConversationRunner — актор диалога:
                           нарратив (персистится в SQLite) + процессы (fg/bg, в памяти)
                                         |
                     AgentLoop.stream() — поток событий: токены, вызовы скилов,
                     финал, отмена; LoopControl — инъекции и отмена прогона
                        /                                 \
         LLMClient (OpenAI-совм., SSE)         SkillRegistry — базовые скилы:
                                               http_request, task_spawn/list, cron_*,
                                               skills_search, instruction_save, external_call,
                                               data_*, memory_*, web_search
                                         |
    instructions · datasets · memory · cron · tasks — SQL-модули ядра (SQLite)
```

Ключевые точки расширения — порты-Protocol: `LLMClient`, `EmbeddingClient` (локальный
sentence-transformers или OpenAI-совместимый эндпоинт), `RerankerClient`,
`MessageRouter`, `PromptProvider`, `CronStore`/`CronWaker`, `TaskStore`/`TaskSpawner`.
Реализации собираются снаружи через DI в composition root — ядро ничего не создаёт
внутри себя.

## Быстрое развёртывание

Требования: Python ≥ 3.11. Для локальных эмбеддингов — доступ к Hugging Face
(модели кэшируются в `~/.cache/huggingface`).

```bash
make install          # создать .venv и поставить оба проекта (editable, с dev-зависимостями)
cp .env.example .env  # заполнить OF_LLM_API_KEY (и при желании OF_LLM_BASE_URL / OF_LLM_MODEL)
make run              # uvicorn с автоперезагрузкой
```

Открыть http://127.0.0.1:8000 — чат-UI со стримом ответов. Поле «Как вас зовут?»
задаёт `user_id` (аутентификации пока нет — это доверенная строка).

Telegram-бот (опционально): задать `OF_TELEGRAM_BOT_TOKEN` в `.env` — бот поднимется
вместе с web. Либо отдельно, без HTTP API и открытого порта (только исходящие
соединения, long polling):

```bash
make run-telegram     # python -m octoforge_web.telegram
```

Для прода: запускайте `uvicorn octoforge_web.main:app` без `--reload` одним процессом
(SQLite — один писатель), следите за `/health` (liveness) и `/health/ready`
(readiness, проверяет БД).

### Конфигурация

Полный список с комментариями — в [.env.example](.env.example). Основное:

| Переменная | Назначение |
|---|---|
| `OF_LLM_BASE_URL` / `OF_LLM_API_KEY` / `OF_LLM_MODEL` | OpenAI-совместимый LLM-эндпоинт (подойдёт и локальный ollama) |
| `OF_DATABASE_URL` | async SQLAlchemy URL, по умолчанию `sqlite+aiosqlite:///./octoforge.db` |
| `OF_EMBEDDING_BACKEND` | `local` (sentence-transformers в процессе) или `openai` (HTTP-эндпоинт) |
| `OF_TELEGRAM_BOT_TOKEN` | токен бота от @BotFather; пусто = адаптер выключен |
| `OF_SERPER_TOKEN` | поиск в вебе через serper.dev; пусто = скил `web_search` выключен |
| `OF_MAX_PROCESSES` / `OF_ROUTER_TIMEOUT_SECONDS` | лимит процессов на диалог / таймаут LLM-роутера |
| `OF_SYSTEM_PROMPT_SOURCE` / `OF_ROUTER_PROMPT_SOURCE` | переопределение промптов из файлов (`file:`, перечитывается на каждый ход) |

Без рабочего бэкенда эмбеддингов приложение стартует (сидирование инструкций
пропускается), но поиск/сохранение инструкций и создание датасетов недоступны.

## Использование как библиотека

`octoforge-core` — самостоятельный пакет без веб-зависимостей (httpx, SQLAlchemy,
Alembic, croniter, sentence-transformers; помечен `py.typed`):

```bash
pip install -e core        # из корня репозитория; FastAPI не потребуется
```

Уровни встраивания — на выбор:

- **`AgentLoop`** — минимум: событийная петля «LLM ↔ скилы» без БД и диалогов.
  Нужны только `LLMClient` и `SkillRegistry`.
- **`ConversationManager`** — полноценные диалоги: персист в SQLite, фон/форграунд
  процессы, LLM-роутер, подписка на события. Пример ниже.
- **Полный набор** — добавить модули (instructions, datasets, memory, cron) и базовые
  скилы по образцу composition root'а — `runtime()` в
  [web/src/octoforge_web/main.py](web/src/octoforge_web/main.py) — это эталонная
  сборка всех зависимостей ядра.

Минимальный диалог с персистом (CLI-поверхность за ~50 строк):

```python
import asyncio

import httpx
from octoforge_core import (
    AgentLoop,
    ConversationManager,
    DialogRepository,
    Failed,
    Finished,
    LLMConfig,
    MessageRepository,
    SkillRegistry,
    SqlAlchemyTaskStore,
    TextDelta,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import StaticPromptProvider
from octoforge_core.agent.router import LLMRouter
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.llm.openai import OpenAICompatibleClient

BASE_URL = "https://api.openai.com/v1"


async def main() -> None:
    engine = create_engine("sqlite+aiosqlite:///./agent.db")
    await init_db(engine)  # в продуктовом коде — bootstrap_schema(engine) (Alembic)
    session_factory = create_session_factory(engine)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL) as http:
            llm = OpenAICompatibleClient(
                http_client=http,
                config=LLMConfig(api_key="sk-...", model="gpt-4o-mini"),
            )
            prompts = StaticPromptProvider()  # встроенные промпты; свои — через порт
            manager = ConversationManager(
                config=RunnerConfig(
                    loop=AgentLoop(llm_client=llm, registry=SkillRegistry(), max_iterations=10),
                    prompts=prompts,
                    router=LLMRouter(llm, timeout_seconds=10.0, prompts=prompts),
                    max_processes=5,
                    compactor=NoopContextCompactor(),  # без сжатия истории
                ),
                dialogs=DialogRepository(session_factory),
                messages=MessageRepository(session_factory),
                tasks=SqlAlchemyTaskStore(session_factory),
            )

            runner = await manager.get_or_create_runner("user-1", "cli")
            events = runner.subscribe()  # подписка — ДО submit, иначе события потеряются
            await runner.submit("Привет! Что ты умеешь?")
            while True:
                event = await events.get()
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, Finished):
                    break
                elif isinstance(event, Failed):
                    print(f"\nОшибка: {event.error}")
                    break
            await runner.stop()
    finally:
        await engine.dispose()


asyncio.run(main())
```

Замечания к примеру:

- С пустым `SkillRegistry` агент отвечает только текстом. Скилы регистрируются по одному
  (`registry.register(HttpRequestSkill(...))` и т.д.) — полный набор
  смотрите в `runtime()`; эмбеддинги нужны только скилам инструкций и датасетов.
- Диалог переживает рестарт: нарратив перечитывается из БД при
  `get_or_create_runner` (процессы — в памяти и не переживают).
- Крон-планировщик — отдельный asyncio-цикл `CronScheduler` поверх `CronStore`;
  выстрел доставляется через порт `CronWaker` (в процессе — `ManagerCronWaker(manager)`).

## Разработка

```bash
make check  # ruff (lint + format) → mypy --strict → pytest для обоих проектов
```

Отдельно: `make lint`, `make typecheck`, `make test`, `make format`.

## Документация

- [docs/design.md](docs/design.md) — живой дизайн-документ: концепция, петля, процессная модель, API.
- [docs/plan.md](docs/plan.md) — дорожная карта этапов A–G (выполнена) и что за рамками.
- [docs/roadmap.md](docs/roadmap.md) — консолидированный бэклог доработок.
- [docs/process-model.md](docs/process-model.md), [docs/cron.md](docs/cron.md),
  [docs/instructions.md](docs/instructions.md), [docs/data-store.md](docs/data-store.md),
  [docs/dialogs.md](docs/dialogs.md) — углублённые темы.
- [docs/prompt-caching.md](docs/prompt-caching.md) — как работает KV-cache префикса у LLM-провайдеров
  и как строить контекст для максимального попадания в кеш.
- [AGENTS.md](AGENTS.md) — конвенции кода и правила workflow.
