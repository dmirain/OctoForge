# Крон-задачи (периодические задания)

> **Статус: реализовано (этап F).** Агент управляет задачами нативными скилами
> `cron_create`/`cron_list`/`cron_delete`/`cron_pause`/`cron_resume` (поверх `CronStore`,
> без HTTP); раньше — через `external_call` по сид-тулам крон-API, см. историю ниже.

## Сценарий

«Готовь каждое утро отчёт» → агент вызывает скил `cron_create` (сид-сценарий
`schedule_a_recurring_report` подсказывает cron-выражение и таймзону) → запись в
`cron_jobs` → планировщик в срок будит диалог (in-process wake) → фоновый процесс актора
(этап E), помеченный `cron_job_id` в `Task.input` → отчёт приходит уведомлением
о завершении.

## Крон-сервис

Подсистема приложения (обособленный модуль `octoforge_core/cron/`; вынос в отдельный
процесс возможен без смены механизмов — порты `CronStore`/`CronWaker` транспортной
формы): таблица `cron_jobs`, цикл планировщика, HTTP API для внешних клиентов. Для
агента — нативные базовые скилы `cron_*` (`skills/basic/cron_jobs.py`), работающие на
любой поверхности, включая standalone Telegram-раннер без HTTP-слушателя (раньше агент
вызывал собственное API через `external_call` — на standalone это падало с
connection refused на `127.0.0.1:8000`; сид-тулы крон-API удаляются миграцией
`migrate_cron_tools_to_native` при старте).

Сам движок планирования тоже за портом: `Scheduler` (`cron/api.py`,
`run_forever()`; `CronScheduler` — реализация по умолчанию). Альтернативные
движки (Celery beat, APScheduler, OS cron) подключаются двумя путями: подмена
`Scheduler` в composition root, либо наш планировщик не стартует вовсе, а внешний
раннер драйвит публичный контракт выстрелов — `CronStore.list_due`/`claim`/
`release_claim`/`complete_fire` + математика расписаний `compute_next_fire`/
`count_missed` (объявлены публичными в `cron/api.py`).

- **cron_jobs**: `id`, `user_id`, `channel`, `title`, `schedule` (cron), `timezone` (IANA,
  default UTC), `prompt`, `enabled`, `next_fire_at`, `last_fire_at`, `claimed_by`,
  `claimed_at`, `created_at`. Все `*_at` — UTC; таймзона — для вычисления расписания
  («утро» — локальное утро юзера). Индексы: `user_id`; `(enabled, next_fire_at)` для
  due-выборки. (`claimed_at` — добавлено к исходному списку колонок: нужно для TTL аренды.)
- **API** (скоуп по `X-User-Id`): `POST /jobs`, `GET /jobs`, `DELETE /jobs/{id}`,
  `POST /jobs/{id}/pause|resume`. Параметры — только query string (исторически: первичным
  вызывающим был агент через `external_call`, который не умеет тела запросов; теперь API
  — для внешних клиентов, агент ходит через скилы). Служебная авторизация —
  whitelist-запись в composition root; служебные токены — позже, с аутентификацией.

## Выстрел

Планировщик (`CronScheduler`, asyncio-цикл с периодом `OF_CRON_POLL_INTERVAL_SECONDS`)
выбирает due-задачи, захватывает их CAS'ом и будит диалог. Standalone: in-process
ярлык — `ManagerCronWaker` → `ConversationManager.wake` → фоновый процесс в диалоге
(см. [process-model.md](process-model.md)) с `prompt` задачи → завершение → уведомление
пользователю. Distributed: HTTP `POST {core}/dialogs/{key}/wake` со служебным токеном —
отложено до аутентификации; вызов через LB, хэш-ключ → инстанс-владелец
(см. [scaling.md](scaling.md)).
Выстрел подчиняется лимиту процессов; переполнение → системная заметка в диалоге
(отложенное уведомление о невозможности запуска, `CRON_LIMIT_NOTE_TEMPLATE`).

## Надёжность

- Exactly-once: CAS-захват due-задач одним UPDATE (`claimed_by` + `claimed_at`, lease
  TTL `OF_CRON_LEASE_TTL_SECONDS`; протухшая аренда перехватывается живым инстансом).
- Пропущенные выстрелы: coalesce до одного с пометкой в prompt («[N scheduled runs were
  missed and coalesced into this one.]»; N — слоты расписания в (last_fire_at|created_at,
  now] минус текущий, cap 100).
- Догонялка (openclaw-review п.5): разброс `REPLAY_STAGGER_SECONDS` (0.5 с) между
  выстрелами тика + лимит реплея на тик (`OF_CRON_REPLAY_LIMIT`, default 5) — остаток
  дожидается следующих тиков.
- `next_fire_at` пересчитывается после каждого выстрела **от момента выстрела** (не
  накапливает дрейф и не порождает серию после простоя); `resume` тоже пересчитывает от
  now — мгновенной догонялки после паузы нет.
- Ошибка доставки wake (исключение из `CronWaker`) → аренда снимается (`release_claim`),
  задача остаётся due и повторяется на следующем тике.

## Принятые решения при реализации

- **Нативные скилы вместо своего HTTP API** — агент управляет задачами скилами
  `cron_*` над `CronStore`: одинаково работает на всех поверхностях (включая standalone
  без HTTP-слушателя); HTTP API остаётся для внешних клиентов.
- **Query-param API** — у `external_call` нет тела запроса (зафиксировано выше).
- **In-process wake в standalone** — HTTP wake endpoint и служебные токены отложены до
  аутентификации.
- **Колонка `claimed_at`** — TTL аренды (добавлена к исходной схеме).
- **Миграция сидов** `migrate_cron_tools_to_native(service)` — удаляет HTTP-сид-тулы
  (`cron_create_job` и др., через новый `InstructionService.delete`) и обновляет
  скил-сценарий `schedule_a_recurring_report` на нативные скилы; идемпотентна.
  Механизм allowlist-авторизации `external_call` (`{user_id}` в заголовке, SSRF-prefixes)
  остаётся для прочих внутренних тулов.
- **Конфиг**: `OF_SELF_BASE_URL`, `OF_CRON_POLL_INTERVAL_SECONDS`,
  `OF_CRON_LEASE_TTL_SECONDS`, `OF_CRON_REPLAY_LIMIT`.

## Известные доработки

- **Путаница крон/фон в промпте** — агент не всегда различает фоновую задачу
  (`task_spawn`) и крон-задачу; граница в системном промпте описана размыто
  (`agent/prompts.py:49` против `:71-74`). Нужны явные правила и примеры.
- **Нет ретрая и статуса фейла выстрела** — планировщик считает выстрел состоявшимся
  в момент спавна процесса; итог процесса (например, «iteration limit») до `CronStore`
  не доходит. Уведомление о фейле в диалог уже приходит системной заметкой — сохранить.
- **Нет идемпотентности `cron_create`** — повторный вызов с теми же аргументами создаёт
  дубль задачи (unique-констрейнта и dedup нет).

Подробности и остальной бэклог — [roadmap.md](roadmap.md).
