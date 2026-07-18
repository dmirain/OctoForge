# Крон-задачи (периодические задания)

> **Статус: реализовано (этап F).** Модель вызова — через инструкции, см. [instructions.md](instructions.md).

## Сценарий

«Готовь каждое утро отчёт» → агент находит tool-запись крон-API через `instructions_search`
(сид `cron_create_job`) → создаёт задачу через `external_call` → запись в `cron_jobs` →
планировщик в срок будит диалог (in-process wake) → фоновый процесс актора (этап E),
помеченный `cron_job_id` в `Task.input` → отчёт приходит уведомлением о завершении.

## Крон-сервис

Подсистема приложения (обособленный модуль `octoforge_core/cron/`; вынос в отдельный
процесс возможен без смены механизмов — порты `CronStore`/`CronWaker` транспортной
формы): свой HTTP API, таблица `cron_jobs`, цикл планировщика. Для агента — внешний
сервис, описанный **тулами в сторе инструкций** (сид-записи): никаких выделенных скилов
`cron_create/list/delete` — агент вызывает API через `external_call`.

- **cron_jobs**: `id`, `user_id`, `channel`, `title`, `schedule` (cron), `timezone` (IANA,
  default UTC), `prompt`, `enabled`, `next_fire_at`, `last_fire_at`, `claimed_by`,
  `claimed_at`, `created_at`. Все `*_at` — UTC; таймзона — для вычисления расписания
  («утро» — локальное утро юзера). Индексы: `user_id`; `(enabled, next_fire_at)` для
  due-выборки. (`claimed_at` — добавлено к исходному списку колонок: нужно для TTL аренды.)
- **API** (скоуп по `X-User-Id`): `POST /jobs`, `GET /jobs`, `DELETE /jobs/{id}`,
  `POST /jobs/{id}/pause|resume`. **Параметры — только query string**: первичный
  вызывающий — сам агент через `external_call`, который рендерит URL, но не умеет тела
  запросов. Служебная авторизация — whitelist-запись в composition root; служебные
  токены — позже, с аутентификацией.

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

- **Query-param API** — у `external_call` нет тела запроса (зафиксировано выше).
- **In-process wake в standalone** — HTTP wake endpoint и служебные токены отложены до
  аутентификации.
- **Колонка `claimed_at`** — TTL аренды (добавлена к исходной схеме).
- **Агент ходит в своё API как во внешнее**: SSRF-гвард получил allowlist-префиксы
  (`SsrfGuard(allowed_prefixes=...)` — пропуск до resolve; в composition root только
  собственный `OF_SELF_BASE_URL`); авторизация — whitelist-запись с темплейтом `{user_id}`
  в значении заголовка `X-User-Id` (подстановка из `SkillContext.user_id`; вызов без
  user_id → без заголовка).
- **Сид тулов** `seed_cron_tools_if_absent(service, base_url)` — маркер
  `cron_create_job`, независимый от weather-маркера базового сида; записи:
  create/list/delete/pause/resume + скил-сценарий `schedule_a_recurring_report`.
- **Конфиг**: `OF_SELF_BASE_URL`, `OF_CRON_POLL_INTERVAL_SECONDS`,
  `OF_CRON_LEASE_TTL_SECONDS`, `OF_CRON_REPLAY_LIMIT`.
