# Рекомендации по оптимизации OctoForge

> Рабочая заметка по результатам глубокого анализа кода от 2026-07-30.
> Не является частью документации (`docs/`); после реализации пунктов — удалять
> или переносить в соответствующие страницы документации.
>
> Анализ покрывал целиком `core/src/octoforge_core/` и `web/src/octoforge_web/`
> (~22k строк), включая сторы, миграции и точки интеграции.
> Контекст: один asyncio-процесс обслуживает все диалоги — любой код на
> event loop дороже ~10 мс замораживает всех пользователей; БД на двух
> диалектах (SQLite/Postgres); векторный поиск — центральная механика.

---

## 1. Баги корректности (приоритет выше оптимизаций)

### 1.1 HTTP-ошибка на старте LLM-стрима минует retry — ВЫСОКИЙ
- `core/src/octoforge_core/llm/openai.py:84` вызывает `raise_for_error_status(response)`
  внутри `async with self._http.stream(...)`; `errors.py:148-151` читает тело через
  `response.json()`, ловя только `ValueError`.
- У реального (непрочитанного) streaming-ответа httpx бросает `ResponseNotRead`
  (`RuntimeError`, не `ValueError` и не `httpx.HTTPError` — проверено
  экспериментально на httpx 0.28.1). Итог: 429/500 на старте стрима вылетает
  как сырой `RuntimeError` — ни типизации, ни ретрая (`RetryingLLMClient.stream`
  ловит только `LLMError`, `llm/retry.py:130`).
- Тесты маскируют проблему: `core/tests/test_openai_stream.py` использует
  `MockTransport` с прикреплённым при конструировании контентом.
- **Фикс:** `await response.aread()` при статусе ≥ 400 перед разбором тела
  (сохраняет provider message), либо ловить `httpx.ResponseNotRead` и
  классифицировать по статусу без тела. Добавить тест на HTTP-ошибку в стриме.

### 1.2 `compact_now` пробрасывает исключение из фонового суммаризатора — СРЕДНИЙ
- `core/src/octoforge_core/context/compactor.py:187` (`await task`), вызов —
  `agent/runner.py:1769`. Падение `_compact` (ошибка LLM-суммаризации, БД)
  убивает ран сырым исключением вместо контролируемого `_fail_run`.
  Протокол `ContextCompactor.compact_now` (`api.py:106-113`) обещает `bool`, не raise.
- **Фикс:** обернуть `await task` в `try/except Exception` (как уже сделано для
  `CancelledError` строкой выше) и вернуть `False`.

### 1.3 Аудит `publish_instruction` всегда пишет UNKNOWN target — НИЗКИЙ, но реальный
- `web/src/octoforge_web/telegram/admin.py:127-133` (`_audit_target`) ищет ключи
  `("instruction_id", "invite_id", "user_id")`, а схема инструмента называет
  параметр `"id"` (`admin.py:89-92`). След «какая инструкция опубликована» теряется.
- **Фикс:** добавить `"id"` в кортеж ключей (или переименовать параметр схемы).

### 1.4 Rate-limiter и аудит ключуются по IP прокси — СРЕДНИЙ
- `Dockerfile:72` запускает uvicorn без `--proxy-headers`, TLS терминирует Caddy
  (`docker/Caddyfile:20`) → `request.client.host` для всех — docker-IP caddy.
- Следствия: `AttemptLimiter` (`auth.py:264-265`) видит всех как одного клиента —
  5 неудачных попыток любого атакующего блокируют консоль для всех на 60 с
  (self-DoS); audit-лог и `get_operator` (`deps.py:116-117`) пишут IP прокси.
- **Фикс:** `--proxy-headers --forwarded-allow-ips=<docker subnet>` в CMD
  (доверяя только сети compose), либо явно читать `X-Forwarded-For`.

### 1.5 Сообщения за время даунтайма бота безвозвратно теряются — СРЕДНИЙ
- `web/src/octoforge_web/telegram/poller.py:901-909` (`_drain_backlog`):
  `get_updates(offset=-1, timeout=0)` забирает только последний update и
  выставляет offset — Telegram подтверждает ВСЕ накопленные update'ы.
- **Фикс:** обрабатывать backlog честно (dedup по `client_message_id` уже есть —
  повторная доставка безопасна); при превышении порога (>50) дренировать с warning.

### 1.6 Неограниченное чтение тел ответов — ВЫСОКИЙ (OOM-вектор)
- `core/src/octoforge_core/net/tools.py:200-202` (`HttpRequestTool`),
  `core/src/octoforge_core/net/external.py:135-136` (`ExternalCallExecutor`):
  `response.text` читает всё тело в память и декодирует на event loop, обрезка
  (`MAX_RESPONSE_CHARS`) — после. Агент может дёрнуть URL, отдающий гигабайты.
- `web/src/octoforge_web/telegram/client.py:175-194` (`_download_once`): кап
  `MAX_DOWNLOADED_FILE_BYTES` проверяется ПОСЛЕ полного скачивания (файл 20 МБ
  при капе 12 МБ будет буферизован целиком).
- **Фикс:** `http.stream(...)` + проверка `Content-Length` до чтения +
  `aiter_bytes` с жёстким байтовым лимитом и обрывом на превышении.

### 1.7 Дрейф типа колонки `tasks.delivered_at` — СРЕДНИЙ
- Миграция `d1e5f9a3b247:45` создаёт колонку как `sa.DateTime()`, модель
  использует `UTCDateTime` (`tasks/models.py:35`). Мигрированные Postgres-базы
  имеют `timestamp without time zone`, свежие (через `_create_and_stamp`,
  `db/engine.py:179`) — `timestamptz`. Постоянный ложный сигнал для
  `compare_type=True` (`env.py:43,54`).
- **Фикс:** новая append-only миграция с `ALTER COLUMN ... TYPE timestamptz`
  (диалект-условная).

### 1.8 Каскадное удаление диалога не атомарно — НИЗКИЙ/СРЕДНИЙ
- `web/src/octoforge_web/api/admin.py:158-164`: `summaries.delete_for_dialog` →
  `tasks.delete_for_dialog` → `exchanges.delete_for_dialog` → `dialogs.delete` —
  каждый вызов своя транзакция. Падение посередине оставляет сирот.
- **Фикс:** одна транзакция на каскад или `ON DELETE CASCADE` на FK.

### 1.9 Сломан документированный путь «агент → external_call → /api/cron» — СРЕДНИЙ
- Докстринг `api/cron.py:1-7` и `docs/reference/http-api.md:43-44` говорят, что
  primary caller — агент через `external_call`; whitelist для self-URL инжектирует
  только `X-User-Id` (`main.py:625-632`), но middleware требует Basic для всего,
  кроме open-paths (`main.py:404-412`), а `/api/cron` не open → 401.
  De facto роутер operator-only (агент управляет cron через внутренние тулзы).
- **Фикс:** признать `/api/cron` operator-only (поправить docstring, доки,
  комментарий `main.py:193-195`) — или дать loopback-вызовам сервисный токен.
  Требует ручной проверки (выведено статически).

### 1.10 Прочие мелкие баги
- Мёртвый `return None` после `return ()` — `agent/runner.py:1141` (нарушает
  объявленный тип `tuple[Attachment, ...]`). Удалить строку 1141.
- `get_or_create` диалога без обработки гонки — `dialogs/store.py:56-64`:
  check-then-insert; два конкурентных первых контакта → необработанный
  `IntegrityError`. Перехват с повторным чтением или `ON CONFLICT DO NOTHING` (PG).
- Ретраи заведомого дубликата idempotency-ключа — `dialogs/store.py:131-166`:
  цикл ловит любой `IntegrityError`; дубликат `uq_messages_dialog_client_message`
  бессмысленно повторяет INSERT 5 раз. Различать нарушение `(dialog_id, seq)`
  (ретрай) и `(dialog_id, client_message_id)` (сразу raise/return existing).
- Нет валидации длины `PostMessageRequest.content` — `api/schemas.py:18`
  (`Field(min_length=1, max_length=...)`).
- Падение embedder'а убивает recall целиком — `instructions/local.py:117`:
  в `save` есть lenient-путь, в `_search` исключение пробрасывается. Деградировать
  к поиску по точному совпадению title (`EXACT_TITLE_BOOST` уже есть).
- Serper: `response.json()` вне try (`search/serper.py:32`),
  `arguments["query"]` без проверки (`search/tools.py:49` → KeyError вместо
  `ToolArgumentsError`).
- Смерть форвардера моста молчит — `telegram/bridge.py:122`: нет done-callback;
  не-TelegramApiError ошибка убьёт форвардер без строки в логе. Добавить
  `add_done_callback` по образцу `_report_worker_exit` (`poller.py:917-923`).
- Сбой submit проглатывается без ответа пользователю — `poller.py:462-474`
  (`_handle_safely`): при падении `runner.submit` сообщение исчезает молча.
  Best-effort уведомление в чат.
- Shutdown не дожидается воркеров — `poller.py:335-338`: после cancel добавить
  `await asyncio.gather(*workers, return_exceptions=True)`.
- Fallback миграций скрывает сбой схемы — `web/main.py:447-451`: любое исключение
  Alembic → `create_all` и молчаливый старт на неправильной ревизии. Как минимум
  логировать ERROR с маркером и/или отражать в `/health/ready`.
- `waker.wake` без таймаута — `cron/scheduler.py:97-103`: зависший wake
  последовательно задерживает все due-джобы тика. `asyncio.wait_for` +
  `release_claim`.

---

## 2. Производительность — высокий эффект

### 2.1 Двойной последовательный embed одного запроса на каждый `recall`
- `core/src/octoforge_core/instructions/tools.py:148-152`: сначала
  `service.search` (embed внутри, `local.py:117`), потом `datasets.search`
  (второй embed того же текста, `datasets/service.py:119`). Recall идёт почти на
  каждое сообщение пользователя — удвоенная латентность и стоимость API-вызовов.
- **Фикс:** минимум — `asyncio.gather` двух поисков; лучше — эмбеддить запрос
  один раз на уровне тулзы и пробрасывать вектор в оба сервиса (или общий кэш
  query-embedding в embedder'е).

### 2.2 Нет eviction простаивающих runner'ов — неограниченный рост памяти
- `core/src/octoforge_core/agent/runner.py:2123-2126` (`ConversationManager._runners`,
  `._builds`). Runner (actor task + нарратив в памяти + подписки compactor'а)
  живёт до `stop_all` или удаления диалога админом (`runner.py:2290` —
  единственный eviction). Cron-`wake` (`runner.py:2183-2196`) создаёт runner
  даже для диалога, в который никто не писал. Завершившиеся build-task'и в
  `_builds` тоже не удаляются (`runner.py:2142`).
- В связке с неограниченными `_inbox` (`runner.py:481`) и `_pending_deliveries`
  (`runner.py:464`) — линейный рост RAM и asyncio-task'ов по числу пользователей.
- **Фикс:** TTL/LRU-eviction простаивающих runner'ов (нет процессов/подписчиков/
  live exchanges → `runner.stop()` + удаление из мап; `_build_runner` умеет
  пересобирать из БД); `done_callback`, убирающий запись из `_builds`; maxsize
  на inbox и cap на outbox («хранить N последних доставок»).

---

## 3. Производительность — средний эффект

### 3.1 O(n²) рендер черновика Telegram на event loop
- `web/src/octoforge_web/telegram/bridge.py:294-306` (`_flush_draft`): каждый
  flush заново прогоняет `markdown_to_telegram_html` и `split_html_safe` по всему
  накопленному буферу. `split_html_safe` дополнительно квадратичен
  (`markdown.py:150-157`, `_open_tags` пересчитывается в цикле отката).
  Главный источник CPU-блокировки loop'а в telegram-модуле.
- **Фикс:** конвертировать только «хвост» буфера (запечатанные чанки стабильны)
  или `to_thread` при буфере > порога (~8 КБ); `_open_tags` — один проход.

### 3.2 N+1 и лишние транзакции на бурстах форвардов
- `runner.py:1018-1023` (`_reparent_material`): `set_exchange` на каждое
  сообщение, каждый — своя сессия + commit (`dialogs/store.py:260-266`).
  Альбом из 20 сообщений = 20 round-trip'ов. Бонус: bulk-вариант покроет и
  краевую некорректность — старые сообщения вне горячего хвоста навсегда
  остаются привязанными к отменённому exchange.
- `runner.py:867-887` (`_collect_material`): `append` + `set_exchange` + `touch`
  = 3 транзакции на material-сообщение (60 на альбом).
- `runner.py:879-881`: `set_exchange` и `touch` — два коммита, объединить
  в одну транзакцию (например `attach_to_exchange(message_id, exchange_id)`).
- **Фикс:** bulk `update(MessageRow).where(MessageRow.id.in_(ids))`; collect —
  принимать `exchange_id` сразу в `append` (известен до вставки).

### 3.3 Дублирующиеся `list_live` на каждый submit
- Один submit: `_route` (`runner.py:1039`) → `_nudge_stale_exchanges`
  (`runner.py:1535`) → `_live_exchange_ids` (`runner.py:1665-1677`) — до 3
  одинаковых SELECT'ов exchanges, плюс `list_live` в `_material_home`
  (`runner.py:901-905`, вызывается даже при заведомо пустом `self._processes`).
- **Фикс:** прочитать `list_live` один раз в `_handle_submit` и пробросить;
  ранний выход из `_material_home` при пустых процессах.

### 3.4 `assemble` компактора: 3-4 последовательных round-trip'а на итерацию
- `context/compactor.py:116-127`: `max_seq_to` → `count_after` →
  (`latest_prompt_tokens`) → `list_for_dialog`, каждый в своей сессии.
  `list_for_dialog` вызывается всегда, хотя при `max_seq_to == 0` суммария
  заведомо нет. Для Postgres — 3 RTT вместо 1.
- **Фикс:** пропускать `list_for_dialog` при `NO_COMPACTED_SEQ`; независимые
  запросы — через `asyncio.gather` или одну сессию.

### 3.5 Пишущие «штампы» в горячих путях
- `bump_usage` — коммит на каждый recall (`instructions/local.py:133`,
  `store.py:147-157`); `usage_count` нигде не влияет на ранкинг. Накапливать
  и сбрасывать батчем/дебаунсом или убрать.
- `last_used_at` секрета — коммит на каждый `resolve` (`secrets/store.py:122-123`;
  на SQLite это writer-lock всего процесса). Троттлить (обновлять, если штамп
  старше N минут) или в фон.
- Upsert telegram-профиля на каждое сообщение (`poller.py:489` →
  `invites/store.py:154-176`). Троттлинг до минутной точности
  (кэш last-recorded в памяти поллера).
- Двойной SELECT датасета на каждый `data_put` — `datasets/tools.py:152` +
  `datasets/service.py:72`: передавать уже разрешённый `Dataset` в `add_record`.

### 3.6 base64 изображений на event loop
- `core/src/octoforge_core/vision/client.py:46-72`: фото 5-10 МБ → ~13 МБ
  base64 + JSON-сериализация на общем цикле (десятки мс блокировки всех диалогов).
- **Фикс:** собирать data-URL в `asyncio.to_thread` (по образцу
  `llm/local_embeddings.py:49`).

### 3.7 Файловый SQLite без WAL
- `db/engine.py:97`: pragma только для `:memory:`, а дефолт — файловый SQLite
  (`web/src/octoforge_web/config.py:30`). Без WAL каждый коммит блокирует
  конкурентных читателей.
- **Фикс:** connect-hook для файловых sqlite-URL:
  `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`.

### 3.8 `task_list` тянет всю историю задач и фильтрует в Python
- `tasks/tools.py:146-149` → `store.list(dialog_id)`; таблица tasks никогда не
  чистится → деградация со временем. `_is_visible` (tools.py:217-227) выражается
  в SQL: `kind='run' AND (status IN ('pending','running') OR (status IN
  ('done','failed') AND delivered_at IS NULL))`.
- **Фикс:** метод порта с предикатом в SQL.

### 3.9 Прогрев telegram-мостов при старте последовательный
- `poller.py:230-236` (`TelegramBridgeRegistry.warm`): 3 запроса к БД на диалог,
  поллер стартует только после прогрева (`main.py:784-786`) — время до
  обслуживания растёт линейно от числа пользователей.
- **Фикс:** `asyncio.gather` с семафором (~8) или ленивый прогрев в фоне.

### 3.10 `reembed_missing` — гигантский батч + N отдельных UPDATE'ов
- `instructions/local.py:262-271`: все записи без векторов одним вызовом
  `embed(...)` (риск отказа бэкенда по размеру), затем каждый `set_embedding` —
  своя транзакция (`instructions/store.py:201-213`).
- **Фикс:** чанки по 64-128 + один `UPDATE ... WHERE id IN (...)` в одной сессии.

### 3.11 Полное чтение таблицы с контентом на каждый поиск
- `instructions/store.py:115-134` (`list_with_embeddings`): `SELECT *` включая
  `content` на каждый recall, хотя контент нужен только топ-k хитам.
- **Фикс:** выбирать без `content`/`tags`, дочитывать контент для shortlist
  вторым запросом; либо кэш (instruction_id → embedding, version).

### 3.12 Датасеты: ранкинг на event loop + неэффективный косинус
- `datasets/service.py:121` → `datasets/ranking.py:30-52`: чистый Python-цикл
  без `to_thread` (в instructions ту же работу векторизовали после замера
  «847 ms стоп-земли на 10k записей»). Кандидатов обычно мало, но ловушка
  без защиты от роста.
- `cosine_similarity` (`datasets/ranking.py:17-27`): норма query-вектора
  пересчитывается для каждого кандидата; локальный эмбеддер уже L2-нормализует
  (`local_embeddings.py:56`) — косинус сводится к dot product.

### 3.13 Отсутствующие индексы
- `dialogs.channel` — `dialogs/store.py:74-86, 284-297` фильтрует по нему
  (старт Telegram-поллера, admin); таблица мала, эффект низкий.
- `(dataset_id, created_at)` — `datasets/store.py:140-148` (`query_candidates`)
  фильтрует диапазон и сортирует по `created_at`; индекс только по `dataset_id`
  (`datasets/models.py:40-43`). Append-only миграция.
- Частичный индекс `tasks(status) WHERE delivered_at IS NULL` — `list_undelivered`
  (`tasks/store.py:204-214`), DONE-ветка растёт бесконечно (эффект на стартап).
  Паттерн есть в `f2a6c8d1e935:125-126` (оба диалекта).

### 3.14 Прочие средние/низкие
- Последовательные fetch'и изображений в `look_at_image` — `runner.py:1127-1129`
  → `asyncio.gather`.
- Лишний SELECT `DialogRow` при каждом append — `dialogs/store.py:157-159`
  → один `UPDATE ... updated_at` в той же транзакции.
- Микро-аллокация в `list_live` — `dialogs/store.py:389`
  (`[status.value ...]` пересобирается на каждый вызов) → модульная константа.
- Задача на каждый токен стрима — `loop.py:405-411` (`ensure_future(anext)` +
  `asyncio.wait` на токен) → один reader-task + `asyncio.Queue`.
- 10 последовательных count-запросов — `admin/store.py:45-74` (`totals()`).
- Двойной `count_missed` на джобу за тик cron — `scheduler.py:88, 136`.
- Повторная JSON-сериализация SSE-события на каждого подписчика —
  `api/dialog.py:76` + `api/sse.py:29-31` (эффект минимален при 1 подписчике).
- Непагинированный `telegram_users` — `api/admin.py:171-205` (единственный
  листинг без `clamp_page`).
- Хэш пароля перепарсивается на каждой проверке — `auth.py:118-127` (заодно
  fail-fast на malformed hash при boot).
- typing-индикатор await'ится перед submit — `telegram/bridge.py:151-152`
  (сетевой вызов с таймаутом 30 с) → fire-and-forget через `create_task`.
- N+1 в telegram-админке — `admin.py:254` (`list_for_user` на пользователя),
  `admin.py:307-316` (`set_enabled` на джобу) → пакетные методы CronStore.
- Перепарсивание `_open_tags` — см. 3.1.

---

## 4. Чистка кода (низкий приоритет)

- Мёртвый код: `runner.py:1141` (см. 1.10); `cosine_similarity` в
  `instructions/ranking.py:29-39` (не вызывается; осознанный дубликат в
  `datasets/ranking.py` — оставить); `SqlAlchemySummaryStore.create`
  (`context/store.py:34-49` — метод только для тестов, не в протоколе);
  мёртвый счётчик `success_count` (`instructions/models.py:49`, `api.py:73` —
  нигде не инкрементируется: либо реализовать запись, либо удалить колонку
  отдельной миграцией).
- Дубль блока атрибуции форвардов трижды — `poller.py:812-816, 944-950, 980-986`
  → функция `_attribution(origin)`.
- Вводящее в заблуждение имя `_dispatch_without_vision` (`poller.py:669`,
  используется и для аудио) → `_dispatch_fallback`.
- Коллизия префикса `tg:` — `client.py:13` (user id) и `images.py:22`
  (attachment ref): сменить префикс ref'ов, например на `tgfile:`.
- Дубль `get_by_title` в `save()` — `instructions/local.py:153-154`
  (`_ensure_not_system` + повторный запрос) → возвращать найденную запись.
- Лишний запрос в `MemoryDeleteTool._find_own` — `memory/tools.py:119-125`.
- Дублирование UPDATE в `record_fire_result` — `cron/store.py:174-197` → один
  statement с условно собранным dict.
- Дрейф docstring'а в `agent/prompts.py:8` (`{processes}` vs реальный
  `{exchanges}`).
- `TaskStore` живёт в `tasks/store.py`, а не `tasks/api.py` — отступление от
  конвенции «соседи импортируют только api.py» (`runner.py:67`,
  `composition.py:58`, `tasks/tools.py:28`): перенести протокол или
  задокументировать исключение.

---

## 5. Что сделано хорошо — не трогать

- **Cancel raced against stream** (`loop.py:393-425`) и eager tool execution
  (`loop.py:85-198`) — с измеренными обоснованиями.
- **Watermark/snapshot-инварианты** runner'а и компактора (`runner.py:1630-1663`,
  `1773-1793`, `compactor.py:119-122`) + `_assemble_lock` — подкреплены
  продакшен-инцидентами.
- **Политика доставки событий**: критические не дропаются, `delivered_at` после
  реального попадания в очередь, outbox ждёт подписчика (`runner.py:2020-2084`).
- **Мемоизация сборки runner'а** per-(user, channel) с `shield`
  (`runner.py:2129-2155`) — нет глобального лока.
- **Векторизованный ранкинг с GIL-aware чанкингом** и `to_thread`
  (`instructions/ranking.py:76-118`, `store.py:124-134`,
  `llm/local_embeddings.py:49`) — подкреплён замерами.
- **Таксономия ошибок LLM и retry** (`llm/errors.py`, `llm/retry.py`):
  Retry-After с потолком и джиттером, запрет ретрая стрима после первого события.
- **SSRF-гард** (`net/guard.py`) с честно задокументированным TOCTOU.
- **Secrets**: значение никогда не попадает в DTO/логи/промпт, привязка к хосту,
  header-safety, скраббинг эха из ответа.
- **CAS-лиз cron одним UPDATE** (`cron/store.py:112-137`) + индекс
  `ix_cron_jobs_due` — exactly-once без распределённых блокировок.
- **Атомарный seq через скалярный подзапрос** + идемпотентность через
  `client_message_id` (`dialogs/store.py:131-168`).
- **Auth**: PBKDF2 в `to_thread`, обе половины проверяются всегда, трёхслойная
  защита от флуда (`auth.py:22-28, 277-283`); CSRF до аутентификации.
- **Telegram-поллер**: диспатч только enqueue'ит, тяжёлая работа в per-user
  воркерах; глобальный семафор инжеста; idle-выгрузка воркеров; сбор альбомов
  через очередь; аккуратный Bot API клиент (ретраи с капом, маскирование токена).
- **Индексы на горячих путях**: `ix_exchanges_dialog_status`, unique
  `(dialog_id, seq)` / `(dialog_id, client_message_id)`, partial unique на
  публичных инструкциях (оба диалекта).
- **Dependency rule** соблюдён везде: `core/` не импортирует fastapi; внешние
  клиенты за `Protocol`-портами; осознанные дубли (datasets/instructions ranking,
  admin read model, cron mapping) задокументированы.

---

## 6. Рекомендуемый порядок работ

1. **Баги корректности (раздел 1):** 1.1 (retry на стриме), 1.2 (`compact_now`),
   1.6 (капы на тела), 1.4 (proxy-headers), 1.3 (аудит, одна строка),
   1.5 (backlog), 1.7 (дрейф схемы).
2. **Высокий эффект (раздел 2):** 2.1 (двойной embed в recall), 2.2 (eviction
   runner'ов).
3. **Средний эффект:** 3.7 (WAL), 3.2 (bulk на форвардах), 3.3 (list_live),
   3.4 (assemble), 3.5 (штампы), 3.1 (telegram-рендер), 3.6 (base64).
4. **Индексы и чистка** (3.13, раздел 4) — отдельным проходом, каждая миграция
   append-only по правилам репозитория.

Определение готовности для каждого пункта: `make check` зелёный; для пунктов,
затрагивающих `agent/loop.py`, SSE или `telegram/`, — дополнительно живой прогон
(мокнутые тесты не доказывают корректность, см. AGENTS.md).
