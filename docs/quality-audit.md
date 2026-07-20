# Аудит качества и дорожная карта улучшений (кроме модульности)

> **Статус: P0 реализовано (Волна 1 закрыта); P1/P2 — roadmap.** Широкий обзор точек роста и
> проблемных мест по корректности, наблюдаемости, персистентности, операбельности, тестам,
> производительности, безопасности и масштабированию. Аудит модульности вынесен отдельно —
> см. [modularity.md](modularity.md). Связанные доки: [design.md](design.md),
> [scaling.md](scaling.md), [process-model.md](process-model.md), [cron.md](cron.md).

## Рамка и приоритизация

Проведены три параллельных аудита: тесты/гейты, безопасность/устойчивость, операбельность/масштаб.

**Целевой сценарий: доверенный пилот/команда.** Аутентификация — на уровне клиента/шлюза; фокус
roadmap — **корректность + наблюдаемость + операбельность**. Мультиарендная изоляция и
горизонтальное масштабирование задокументированы, но отложены (станут P0 при переходе в
мультиарендный prod).

- **P0** — баги корректности + наблюдаемость + миграции: ломают систему независимо от доверия среды.
- **P1** — операбельность, тесты/CI, деплой, эвикция памяти, зависимости.
- **P2 (отложено)** — аутентификация, межарендная изоляция, rate-limiting, горизонтальный масштаб,
  производительность поиска. Документируем как «долг перед мультиарендностью».

При реализации: правки сопровождаются обновлением [design.md](design.md); язык — доки/UI русский,
докстринги/комментарии/коммиты английские.

## P0 — Корректность и устойчивость (баги при любом сценарии) ✅ сделано

> A1/A2 закрыты в коммите супервизии актора; A3 — в коммите Alembic. Наблюдаемость и
> readiness — в соответствующих коммитах. Ниже — исходные формулировки проблем.

| # | Проблема | Место |
|---|---|---|
| A1 | **Актор без супервизии.** Необработанное исключение в `_run_actor` (напр. ошибка БД в `_persist`) выходит из корутины, которая создана `create_task` и никогда не ожидается → проглатывается. Диалог становится зомби: `submit()` копит в unbounded-инбокс без потребителя, живо до рестарта. | `core/.../agent/runner.py:246-262`, task launch `:151-154` |
| A2 | **Утечка process-pump.** Очистка (`_finalize` с DB-записью, `_remove_process`, broadcast, `_ProcessTerminated`) — **вне** try в `_pump_process`. Сбой `_finalize` убивает pump, процесс не удаляется из `self._processes`, навсегда занимает слот `max_processes`, результат задачи не доставляется. | `core/.../agent/runner.py:409-435`, `_finalize` `:437-475` |
| A3 | **Нет миграций — только `create_all`.** `init_db` создаёт лишь отсутствующие таблицы, не `ALTER`-ит существующие. Любое изменение схемы на живой БД падает в рантайме без пути миграции. Alembic не подключён. | `core/.../db/engine.py:23-26` |

**Целевые решения.**
- A1: обернуть тело actor-цикла в try/except с логированием + перевод диалога в явное
  failed-состояние (или рестарт актора с backoff); не проглатывать. Добавить `task.add_done_callback`
  для surфейсинга необработанных исключений.
- A2: перенести `_finalize`+cleanup внутрь `try/finally`, чтобы процесс всегда удалялся из
  `self._processes` и слот освобождался даже при сбое DB-записи; ретрай/лог для доставки результата.
- A3: подключить Alembic, первая ревизия = снимок текущей схемы; `init_db` → `alembic upgrade head`
  на старте (или отдельный шаг). Обязательно до первого изменения схемы.

## P0 — Наблюдаемость (слабейшая зона) ✅ базово закрыто

> Логгеры добавлены в `core/` (`runner`, `cron/scheduler`), тихие `except` теперь логируются,
> потеря SSE считается; добавлен readiness `/health/ready` (SELECT 1) и idempotent-конфиг
> логирования веб-приложения. Осталось на P1: структурные JSON-логи, request-id/корреляция,
> метрики/трейсинг.

- **В `core/` ноль логирования** — каждый путь отказа доменной логики нем.
- **Тихие `except`:** cron `_fire` глотает ошибку `waker.wake()`, освобождает lease, `return` без
  лога (`cron/scheduler.py:74-89`) → «крон не сработал» недиагностируемо. Agent loop конвертит краш
  в `Failed`-событие только для живых SSE-подписчиков (`runner.py:424-427`); для фонового/крон-процесса
  без подписчика — исчезает.
- **Лоссовый SSE молча:** `_broadcast` роняет события на `QueueFull` без счётчика (`runner.py:511-513`).
- **Нет корреляции:** ни request-id, ни трейсинга через модель актор→процесс→SSE→cron→task. Веб-логи
  неструктурированы; `basicConfig` только в Telegram-standalone (`telegram/__main__.py:39-41`).
- **`/health` — только liveness** (`main.py:205-207`); нет readiness (БД/эмбеддинги/LLM/scheduler).

**Целевые решения.** Ввести структурное логирование (JSON) в `core/` со стандартным `logging` и
контекстом (`dialog_id`, `process_id`, `user_id`, `cron_job_id`); заменить тихие `except` на
`logger.exception(...)` + метрику; счётчик дропнутых SSE; readiness-эндпоинт с проверками БД/бэкендов;
опционально OpenTelemetry-хуки (за портом, чтобы инсталлятор подключал свой экспортер).

## P1 — Персистентность и БД (частично P0 из-за миграций выше)

- **SQLite single-writer без тюнинга:** нет WAL/`busy_timeout`/pool-настроек (`db/engine.py:13-15`) →
  `database is locked` при конкуренции pump-ов + cron + skill-записей. Добавить `connect_args`
  (`busy_timeout`, WAL pragma), `pool_pre_ping`/`pool_recycle` для будущего Postgres.
- **Недостающий индекс `tasks.status`** — `count_active()` сканирует по статусу на каждый spawn
  (`repositories.py:156-164`).
- **Лишние round-trips:** `session.get` на каждый append сообщения ради `updated_at`
  (`repositories.py:86-88`); 2-запросный get-then-update в финализации задач (`runner.py:451-465`,
  `repositories.py:174-184`). Свернуть в один UPDATE.

## P1 — Операбельность и деплой

- **Нет Dockerfile / compose / prod-раннера / деплой-доков.** `make run` = `uvicorn --reload`
  (dev-режим). Добавить Dockerfile (multi-stage, опц. без torch — см. «Производительность»),
  prod-запуск (uvicorn-workers с учётом ограничения актора — см. «Масштабирование»), healthcheck.
- **Нет graceful drain.** На SIGTERM in-flight LLM-ходы жёстко отменяются (`main.py:319-332`),
  спасается только частичный текст (`_salvage_interrupted_turn`). Добавить drain активных процессов.
- **Осиротевшие задачи после рестарта:** PENDING/RUNNING остаются навсегда (`process-model.md:74-75`).
  Добавить reaper при старте (пометить orphaned RUNNING как failed/повторить).
- **Секреты в рабочем дереве:** `OctoForgeBotToken.txt`, `.env`, `octoforge.db` в корне (gitignored,
  но в дереве). Гигиена: вынести из корня; `octoforge.db` содержит все диалоги/память в открытом виде.

## P1 — Тесты и гейты качества

- **Нет CI** — весь гейт локальный (`make check`). Добавить GitHub Actions с `make check` на обоих
  проектах.
- **Нет измерения покрытия** — подключить `pytest-cov` + порог.
- **Пробелы:** `agent/prompts.py` (поведенческий контракт системного промпта) и `cron/waker.py`
  (`ManagerCronWaker` — мост scheduler→диалог, единственный непокрытый glue) без тестов.
- **Мелочи:** нет `conftest.py` (дублирование фикстур `session_factory`/`StubEmbedder`);
  `--strict-markers`/`filterwarnings=error` не включены; малые реальные `asyncio.sleep` в
  поллинг-тестах — потенциальный флейк под нагрузкой CI.
- **Зависимости:** плавающие нижние границы, нет lock-файла → невоспроизводимые сборки. Запинить,
  добавить lock (`uv.lock`/`pip-tools`).

## P1/P2 — Производительность

- **Brute-force cosine по всей таблице на event-loop:** `list_with_embeddings()` без LIMIT +
  Python-цикл `sum(a*b)` без NumPy (`instructions/store.py:69-79`, `ranking.py:27-37`; datasets
  аналогично, но scoped по owner). Векторный поиск на event-loop блокирует цикл O(rows×dim).
  Решения: NumPy-векторизация + `to_thread`, или перенос поиска в БД (см. P1 в [modularity.md](modularity.md)
  — `search_by_vector`). `MAX_SCAN_ROWS=1000` ограничивает только record-scan, **не** embedding-scan.
- **Обязательный `sentence-transformers`/torch (~1-2 ГБ) даже на OpenAI-бэкенде**
  (`core/pyproject.toml:15`); ленивая загрузка модели тормозит первый запрос. Вынести локальный
  бэкенд в extra (`pip install octoforge-core[local]`), warm-up при старте для LOCAL, CUDA-путь в
  выборе устройства реранкера (`reranker.py:71-75` сейчас только MPS/CPU).

## P2 — Безопасность и изоляция (отложено; станет P0 при мультиарендности)

> При «доверенном пилоте» auth делается на клиенте/шлюзе. Но S2 и S4 стоит закрыть раньше — это
> персистентный долг, не зависящий от auth.

| # | Severity | Проблема | Место |
|---|---|---|---|
| S1 | CRITICAL (для prod) | `X-User-Id` доверяется без аутентификации — вся изоляция на подделываемом заголовке | `web/.../deps.py:35-39` |
| S2 | HIGH (закрыть раньше) | Instructions-хранилище **глобальное** (нет `owner_user_id`): межарендная утечка + отравление tool/knowledge по `(type,title)` | `instructions/store.py`, `local.py` |
| S3 | HIGH | Нет rate-limiting нигде (сообщения, cron, datasets, memory); process-cap только per-dialog, не глобальный | `web/` + `runner.py:337`, `config.py:23` |
| S4 | MEDIUM (закрыть раньше) | Глобальная память писабельна любым агентом → отравление общего контекста | `skills/basic/memory_store.py:56`, `memory/store.py:77` |
| S5 | MEDIUM | Prompt-injection: недоверенные memory/instructions/HTTP-body/cron-prompt подаются агенту с write-тулзами без tool-gating | `agent/loop.py:148-150`, `scheduler.py:96` |
| S6 | MEDIUM | SSRF DNS-rebinding TOCTOU (задокументировано, не закрыто); `self_base_url`-allowlist → prompt-injection достаёт внутренний API (в рамках того же user) | `net/guard.py:9-13`, `main.py:134,256-263` |
| S7 | LOW | Task-store lookups без owner-check (латентный IDOR, если появится task-эндпоинт) | `db/repositories.py:111-172` |

## P2 — Горизонтальное масштабирование (отложено)

- **`ConversationManager._runners` in-memory, без эвикции** + полный нарратив грузится и растёт
  неограниченно (`runner.py:545,558,142`) → утечка памяти; при подделываемом `X-User-Id` — DoS
  распуханием runner-ов.
- **Split-brain при нескольких воркерах/репликах:** у каждого свой in-memory менеджер; без
  sticky-routing два актора на диалог пишут в `messages`/`tasks`. Cron CAS-lease защищает планировщик,
  но wake доставляется в локальный менеджер → второй актор. [scaling.md](scaling.md) описывает
  consistent-hash routing — **не реализовано**.
- **Ближайший дешёвый шаг (даже для пилота):** LRU/TTL-эвикция runner-ов + ограничение длины
  загружаемого нарратива (window/пагинация) — снимает утечку памяти без внедрения роутинга.

## Дорожная карта (порядок волн)

**Волна 1 (P0, корректность+наблюдаемость): ✅ сделано.** A1 супервизия актора → A2 фикс утечки pump
→ A3 Alembic → логгеры в `core/` + замена тихих `except` → readiness `/health/ready` + конфиг
логирования → CI с `make check` (GitHub Actions). Осталось на P1: структурные JSON-логи и
корреляция (request-id), метрики.

**Волна 2 (P1, операбельность+тесты):** WAL/`busy_timeout`/pooling + индекс `tasks.status` → Dockerfile
+ prod-раннер + graceful drain + reaper осиротевших задач → pytest-cov + тесты `prompts.py`/`waker.py`
+ `conftest.py` → пиннинг зависимостей/lock → вынос torch в extra.

**Волна 3 (P1/P2, persistent-долг изоляции + перф):** `owner_user_id` в instructions (S2) + гейт
глобальной памяти (S4) → LRU/TTL-эвикция runner-ов + окно нарратива → NumPy+`to_thread` для cosine
(или `search_by_vector`).

**Волна 4 (P2, при мультиарендности):** аутентификация (замена доверия `X-User-Id`) → rate-limiting +
глобальный process-cap → tool-gating недоверенного контента → consistent-hash routing / sticky
sessions → Postgres-профиль.

## Верификация (для этапа реализации)

- `make check` в обоих проектах (ruff + `mypy --strict` + pytest); добавить в CI.
- A1/A2: тест на «actor выживает при исключении в `_persist`» (fake-репозиторий кидает) и
  «process-слот освобождается при сбое `_finalize`».
- A3: тест миграции — прогнать `alembic upgrade head` на пустой БД и убедиться, что схема совпадает с
  `create_all` (снимок).
- Наблюдаемость: тест, что сбой `waker.wake` и краш agent-loop логируются (caplog), а не глотаются.
- Readiness: интеграционный тест `/health/ready` при недоступной БД/эмбеддингах.
- E2E-дым: `make run` (chat UI на http://127.0.0.1:8000) и `make run-telegram` — дефолтная сборка не
  сломана.

## Сильные стороны (сохранить)

SSRF-гард (приватные/CGNAT/metadata диапазоны + no-redirect на обоих сетевых скиллах), защита от
format-string инъекции в `url_template` (`net/tool_spec.py:113-116`), отсутствие `eval`/`exec`/raw SQL
(ORM-параметризация, экранирование LIKE), надёжный cron (CAS-lease, exactly-once, coalescing
missed-runs + replay-cap, чистый shutdown фоновых задач), корректная owner-изоляция datasets/cron/dialogs,
секреты не логируются (токен бота намеренно вне логов), качественные тесты (моки LLM/HTTP, реальный
SQLite, E2E через `TestClient`).
