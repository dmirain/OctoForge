# AGENTS.md

> Guidance for AI coding agents working in this repository.

## Project status

Событийная петля с актором диалога, персистом в SQLite и фоновыми задачами работает. Реализовано:

- монорепо из двух проектов: библиотека `core/` (`octoforge-core`) и web-приложение `web/` (`octoforge-web`) — у каждого свой `pyproject.toml`, зависимости и тесты;
- петля как поток событий: `AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` — токены, вызовы скилов, финал, отмена;
- управление прогоном: `LoopControl` (инъекции сообщений + отмена с сохранением частичного ответа);
- актор диалога: `ConversationRunner`/`ConversationManager` — ключуются по `(user_id, channel)` (get-or-create), история пересобирается из БД, сообщения персистятся по ходу прогона, проактивные уведомления о задачах;
- персист: SQLAlchemy async + SQLite, пакет `db/` (Base + `UTCDateTime`, ORM-модели dialogs/messages/tasks, фабрики engine/session, репозитории); `create_all` при старте, Alembic — при первой деструктивной миграции;
- фоновые задачи за протоколом `TaskStore` (боевой `SqlAlchemyTaskStore`, `InMemoryTaskStore` — для тестов): `TaskRunner` + скилы `task_spawn`/`task_list`, отметка доставки `result_delivered`;
- скилы: `Skill`/`SkillSpec`/`SkillContext(user_id, channel, dialog_id)`/`SkillRegistry`, типы `BASIC|DYNAMIC`; базовые `http_request`, `task_spawn`, `task_list`, `instructions_search`, `instruction_save`, `external_call`, `data_put`, `data_query`, `data_forget`;
- LLM-клиент: `complete()` + `stream()` (SSE, tools/tool_calls); порт `EmbeddingClient` + OpenAI-совместимый клиент эмбеддингов `llm/embeddings.py` (POST /embeddings, конфиг `EmbeddingConfig` рядом с `LLMConfig`); порт общий для модулей инструкций и датасетов;
- инструкции (этап B): обособленный пакет `instructions/` (граница `api.py` — Protocol `InstructionService`: `search`/`save`/`get_by_name`; локальная реализация — таблица `instructions`, cosine-ранжирование + буст точного title, сидирование); исполнение внешних вызовов — вне модуля: `net/` (`ExternalCallExecutor` поверх tool-записей, `SsrfGuard`, whitelist-авторизация из composition root);
- датасеты (этап C): обособленный пакет `datasets/` (граница `api.py` — Protocol `DatasetService`; локальная реализация — таблицы `datasets`/`dataset_records`, валидация записей по JSON-схеме, cosine-поиск по дескрипторам с бустом точного имени, owner-изоляция на уровне SQL); скилы `data_put` (create-if-absent)/`data_query`/`data_forget`; дескрипторы участвуют в `instructions_search`; см. `docs/data-store.md`;
- web: dialog API (`POST /api/dialog/messages`, `POST /api/dialog/cancel`, `GET /api/dialog/events` SSE) с заголовком `X-User-Id` (доверенная строка, без аутентификации), чат-UI со стримом и кнопкой «Стоп»; канал `"web"` объявлен в composition root.

Не реализовано: пользователи/аутентификация (user_id — доверенная строка от клиента), редоставка недоставленных результатов задач при старте, влияние usage/success-статистики на ранг поиска инструкций и http-реализация фасадов `InstructionService`/`DatasetService` (выделенный сервис), память, роутер и процессная модель (`docs/process-model.md`). План — `docs/design.md`.

## Project overview

OctoForge — мультипользовательский LLM-агент: Python, FastAPI, SQLAlchemy (async, SQLite), скилы на исполняемых Jinja-шаблонах, знания в БД, фоновые задачи с уведомлениями. Детали — `docs/design.md`.

## Repository layout

- `core/` — библиотека `octoforge-core` (src-layout): домен, порты, `agent/` (events/control/loop/prompts/runner), `skills/` (base/registry/basic), `tasks/` (models/store/runner), `llm/` (events/openai/embeddings — включая порт `EmbeddingClient`), `db/` (base/models/engine/repositories), `instructions/` (модуль инструкций: api/models/store/ranking/local/seed), `datasets/` (модуль датасетов: api/models/validation/store/ranking/service), `net/` (SSRF-гвард, исполнитель внешних вызовов). Не импортирует fastapi; sqlalchemy — только в `db/` и SQL-сторах (`db/repositories.py`, `instructions/store.py`, `datasets/store.py`).
- `web/` — приложение `octoforge-web` (src-layout): FastAPI-обёртка, `api/` (dialog/sse/schemas), статика, composition root в `main.py`. Зависит от `octoforge-core`.
- Корень: `Makefile`, `README.md`, `.env.example`, `docs/`.

## Build and test commands

- `make install` — создать `.venv` и поставить оба проекта editable с dev-зависимостями
- `make check` — ruff (lint + format check) → mypy strict → pytest для обоих проектов
- `make lint` / `make typecheck` / `make test` / `make format` — шаги по отдельности
- `make run` — uvicorn с автоперезагрузкой (нужен `.env` с `OF_LLM_API_KEY`)

Конфиги ruff/mypy/pytest — в `core/pyproject.toml` и `web/pyproject.toml` соответственно.

## Code conventions

Обязательны во всём коде проекта:

1. **UTC everywhere** — все даты timezone-aware UTC. Время получаем только через хелпер `utc_now()` (`octoforge_core/time.py`); naive datetime запрещены. На SQLite — TypeDecorator, принудительно выставляющий UTC при чтении/записи.
2. **Полная типизация** — аннотации на всех функциях (аргументы и возвращаемое значение) и атрибутах классов. Контроль: ruff ruleset `ANN` + mypy strict. `Any` только внутри контейнеров (`dict[str, Any]` на JSON-границе), голый `Any` в аннотациях запрещён (ANN401).
3. **Объекты, не словари** — данные носим в доменных объектах (dataclass/pydantic) и ORM-моделях. `dict` допустим только на самой границе (JSON in/out) и сразу валидируется в объект.
4. **Перечисления — Enum** — статусы, виды, роли и т.п. объявляем через `StrEnum`; в БД хранится значение enum'а.
5. **Никакой магии** — строковые и числовые литералы со смыслом — именованные константы или конфиг; лимиты — в конфиге. В тестах HTTP-коды — через `HTTPStatus`.
6. **Тесты в том же изменении** — фича или фикс без тестов не считается сделанным. pytest + pytest-asyncio; внешние системы (LLM, HTTP) мокаем.
7. **Чистая архитектура** — слои: библиотека `core/` (домен, сервисы, порты-Protocol + адаптеры) → приложение `web/` (FastAPI-адаптеры). Зависимости только внутрь: библиотека не импортирует fastapi; внешние клиенты попадают в сервисы через Protocol-порты.
8. **Линтер перед тестами** — порядок проверки: `ruff check` → `ruff format --check` → `mypy` → `pytest`. Одна команда: `make check`.
9. **Контроль сложности** — ruff: `C901` (цикломатическая сложность ≤ 10), `PLR0915` (≤ 50 statements), `PLR0911` (≤ 6 return'ов). Превышение = дробим функцию по ответственности, а не отключаем правило.
10. **Ядро отдельно, web отдельно** — физически разные проекты: `core/` и `web/`. Web — тонкий адаптер, вся логика в библиотеке.
11. **DI (Dependency Injection)** — зависимости приходят снаружи: через конструктор, параметры или `Depends` на web-слое. Создавать зависимости внутри методов запрещено; сборка графа объектов — в одном месте (composition root: `web/src/octoforge_web/main.py` и фабрики).
12. **Языки** — общение с пользователем и вся документация (`docs/`, `README.md`, этот файл) — на русском. Коммиты, docstrings и комментарии в коде — на английском.

## Workflow rules

- **Git-коммиты — только с явного разрешения пользователя.** Спрашивать разрешение каждый раз перед `git commit` и любой другой git-мутацией (push, reset, rebase и т.д.).
- **Документация обновляется вместе с кодом.** Каждое изменение, меняющее логику, и каждая новая идея дописываются в `docs/design.md` (а при смене конвенций, структуры или команд — и в этот файл) в том же изменении.

## Tooling

- ruff — lint + format (rulesets: `E, F, I, UP, B, SIM, ANN, C90, PL, RUF`; line-length 100)
- mypy — проверка типов (`strict = true`)
- pytest + pytest-asyncio — тесты
- Makefile — единая точка входа проверок и запуска

## Documentation

- `docs/design.md` — живой дизайн-документ (на русском): концепция, архитектура петли, API, структура, план реализации.
- `AGENTS.md` (этот файл) — конвенции кода, команды и правила workflow.
