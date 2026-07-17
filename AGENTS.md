# AGENTS.md

> Guidance for AI coding agents working in this repository.

## Project status

Событийная петля с актором диалога и фоновыми задачами работает. Реализовано:

- монорепо из двух проектов: библиотека `core/` (`octoforge-core`) и web-приложение `web/` (`octoforge-web`) — у каждого свой `pyproject.toml`, зависимости и тесты;
- петля как поток событий: `AgentLoop.stream(history, control, context) -> AsyncIterator[LoopEvent]` — токены, вызовы скилов, финал, отмена;
- управление прогоном: `LoopControl` (инъекции сообщений + отмена с сохранением частичного ответа);
- актор диалога: `ConversationRunner`/`ConversationManager` — сериализация команд, broadcast подписчикам, проактивные уведомления о задачах;
- фоновые задачи (in-memory за протоколом `TaskStore`): `TaskRunner` + скилы `task_spawn`/`task_list`;
- скилы: `Skill`/`SkillSpec`/`SkillContext`/`SkillRegistry`, типы `BASIC|DYNAMIC`; базовые `http_request`, `task_spawn`, `task_list`;
- LLM-клиент: `complete()` + `stream()` (SSE, tools/tool_calls);
- web: conversations API (create/messages/cancel) + SSE `events`, чат-UI со стримом и кнопкой «Стоп».

Не реализовано: БД и персист, пользователи/аутентификация, инструкции в БД (знания/скилы/тулы/датасеты + векторный поиск, см. `docs/instructions.md`, `docs/data-store.md`), память, роутер и процессная модель (`docs/process-model.md`). План — `docs/design.md`.

## Project overview

OctoForge — мультипользовательский LLM-агент: Python, FastAPI, SQLAlchemy (async, SQLite), скилы на исполняемых Jinja-шаблонах, знания в БД, фоновые задачи с уведомлениями. Детали — `docs/design.md`.

## Repository layout

- `core/` — библиотека `octoforge-core` (src-layout): домен, порты, `agent/` (events/control/loop/prompts/runner), `skills/` (base/registry/basic), `tasks/` (models/store/runner), `llm/` (events/openai). Не импортирует fastapi/sqlalchemy.
- `web/` — приложение `octoforge-web` (src-layout): FastAPI-обёртка, `api/` (conversations/sse/schemas), статика, composition root в `main.py`. Зависит от `octoforge-core`.
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
