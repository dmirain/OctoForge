# Сравнительное исследование: openclaw vs OctoForge

> Глубокий разбор [openclaw](https://github.com/openclaw/openclaw) (персональный AI-ассистент,
> TypeScript, ~3.35M строк, один gateway-процесс на ~20 каналов) относительно OctoForge —
> по каждому нашему модулю. Зрелость и популярность в расчёт не берутся: только механики
> и архитектура. Исследование по коду (8 параллельных разведок + спот-проверка ключевых
> утверждений). Эта версия **заменяет** ранний поверхностный обзор; механика компакции
> подробно выписана отдельно в `docs/openclaw-compaction.md` и здесь не повторяется.
> Пересечения с `docs/hermes-review.md` и `docs/opencode-review.md` помечены (↔ hermes) /
> (↔ opencode) — совпадения по трём независимым проектам это сильнейшие кандидаты.

## Обзор: две философии

**openclaw** — single-user персональный ассистент владельца устройства: один gateway держит
каналы (Telegram/WhatsApp/Slack/iMessage/…), диалоги, крон, скилы, shell/file-инструменты,
голос, canvas. Мультиюзерность — «по контейнеру на клиента». Стиль: модульные глобальные
синглтоны, файловые транскрипты (JSONL → SQLite миграция), плагинная система с манифестами,
огромный шлейф compat-кода.

**OctoForge** — hosted multi-user диалоговый агент: Protocol-порты, DI из composition root,
asyncio-актор на диалог, fg/bg-процессы, LLM-роутер, SQL с Alembic, mypy strict.

Ключевая асимметрия: у них богатая механика одиночного ассистента (каналы, память, скилы,
exec) при хрупкой архитектуре (глобальные реестры, файловые сторы с самолечением); у нас —
чистая мультиюзерная архитектура при минимальной механике. Почти всё ценное у них —
переносимые идеи, а не код.

---

## agent/ — луп, роутинг сообщений, конкурентность

**У них.** Луп — `src/agents/embedded-agent-runner/run-loop.ts`: не простой reason-act цикл,
а оркестратор ретраев/фейловера (ротация auth-профилей, fallback моделей, ретрай пустых
ответов, idle-timeout прерыватель, guard против runaway после компакции). На сессию один
активный прогон; входящее во время прогона — steer в живой прогон с типизированными причинами
отказа. **Проверено**: ошибки — данными (assistant-сообщение с `stopReason:"error"`, история
не ломается); пометка прерывания (при рестарте gateway в историю инъектится «previous turn
was interrupted…» и сессия резюмится); две очереди (steer vs followup) + **debounce и
склейка всплесков** в один ход (collect-режим), политика переполнения очереди
(new/old/summarize); **идемпотентность** — user-turn хранит `idempotencyKey`, запись
дедупит сканом; хуки `before_tool_call`/`after_tool_call` (block/rewrite/requireApproval).
Сессии: `agent:<agentId>:<channel>:<account>:<peerKind>:<peerId>` + identity-links,
склеивающие человека между каналами.

**У нас.** `AgentLoop.stream()` — async-генератор типизированных событий; актор с инбоксом;
LLM-роутер (INJECT/START_NEW/CANCEL/PROMOTE) с детерминированным фолбэком; salvaged-частичный
ответ с `INTERRUPTED_NOTE`; репорт-прогоны для фоновых завершений.

**Мы лучше:** семантический роутинг смысла сообщения (у них steer-vs-followup — статическая
30-строчная политика; CANCEL/PROMOTE без аналога); актор на диалог против глобальных карт
прогонов (multi-tenant изоляция состояния и фейлов); типизированный ADT событий;
бюджет сложности (loop.py 171 строка + runner.py 668 против run-loop.ts 669 + ~40
run/*-модулей + 2000-строчные хук-файлы).

**Мы хуже:** нет идемпотентности входа (ретрай доставки задвоит сообщение); нет
debounce/склейки (каждое сообщение = вызов роутера); нет ретраев/фейловера в лупе
(транзиентная ошибка = Failed, и она не остаётся в нарративе); нет watchdog'а зависшего
прогона; отмена кооперативна между событиями (скилл не прервать посреди); нет хуков.

**Взять:**
1. **Идемпотентный submit** (`client_message_id` на сообщении, unique-index, skip-if-seen).
   (↔ opencode.) Полдня, высокая ценность.
2. **Debounce + склейка** `_Submit` в акторе (~1–2с, мердж в одно user-сообщение перед
   роутером). Средняя стоимость, высокая ценность: режет вызовы роутера на «пулемётном»
   вводе.
3. **Ретрай транзиентных ошибок в лупе** до `Failed` (классификация + backoff).
   (↔ hermes ↔ opencode.)
4. **Idle-watchdog процесса**: `OF_PROCESS_IDLE_TIMEOUT`, срабатывание → отмена + FAILED-заметка.
5. **Фейлы — данными нарратива**: системная заметка при `Failed` (по образцу
   `INTERRUPTED_NOTE`), чтобы компакция и роутер видели историю целиком.
6. Порт `SkillHooks` (см. раздел скилов). (↔ opencode.)
7. Restart-recovery заметка (в бэклоге; у них — прямой шаблон текста).
**Не брать:** lease/ack протоколы стеринга, lane-приоритеты, overflow-summarize промпты —
single-tenant машинерия.

---

## context/ + db/ — контекст, компакция, хранение

(Механика их компакции — в `docs/openclaw-compaction.md`; здесь новое.)

**У них.** Системный промпт — строгая дисциплина prefix-cache: `stablePrefix + dynamicSuffix`,
граница помечена `SYSTEM_PROMPT_CACHE_BOUNDARY` (проверено, `system-prompt.ts:1287`), стабильная
часть мемоизирована LRU по SHA-256 входов; **текущего времени в системном промпте нет
сознательно** — только таймзона, а время ездит в конверте user-сообщения; Runtime-строка
очищается от волатильных id; файлы контекста — в детерминированном порядке. На транспорте —
`cache_control: ephemeral` только на стабильном префиксе, бюджет 4 брейкпоинта
system→tools→messages. Токен-учёт: `normalizeUsage` сводит все формы провайдеров
(input/output/cacheRead/cacheWrite/reasoning, коррекция двойного счёта). Хранилище:
JSONL-транскрипты с самолечением (quarantine битых файлов) мигрируют в SQLite
(`session_entries`, `transcript_events` с seq, `transcript_event_identities` с ключами
идемпотентности); ретенция — архивация сессий, у сигнальных логов 30-дневный watermark.
Отображение usage в TUI (`tokens 12k/200k`).

**У нас.** Трёхуровневая модель (архив/саммари с темами/горячий хвост `max(seq_to)`),
фоновая компакция, безопасные границы, `history_search`; SQLAlchemy async + Alembic +
`UTCDateTime` + атомарный seq; `_with_current_date` **в системном промпте**.

**Мы лучше:** хранилище радикально проще и безопаснее (одна транзакционная схема против 563
строк JSONL-кэша/repair-машинерии + marker-индирекции); состояние компакции выводится из
данных (нет `firstKeptEntryId`-указателя для консистентности); компакция вне горячего пути;
мультиюзерная изоляция в SQL, а не путями файлов.

**Мы хуже:** **prefix-cache враждебность**: `_with_current_date` в системном промпте бьёт
кэш на каждой новой ветке; нет stable/volatile сплита и cache-маркеров; нет токен-учёта
(↔ hermes ↔ opencode); нет overflow-ретрая; нет ретенции `messages`/`dialog_summaries`;
блок тем растёт без bounds.

**Взять:**
1. **Stable/volatile сплит промпта**: дату из системного промпта — в конверт последнего
   user/system-сообщения (у них: таймзона в промпте, время в конверте). Дёшево, немедленная
   экономия на cache-read. (↔ hermes/opencode prompt-caching.)
2. Usage capture + `deriveContextPromptTokens`-аналог (↔ hermes ↔ opencode) + показ usage
   в web UI.
3. Overflow → синхронная компакция → один ретрай (↔ hermes ↔ opencode).
4. Ретенция архива: чистка строк, покрытых саммари и старше N дней (их 30-дневный
   watermark-паттерн). Малая стоимость, средняя ценность.
5. `cache_control` маркеры — когда появится Anthropic-бэкенд; LRU не нужен.
**Не брать:** JSONL-транскрипты, гигантский `ContextEngine` интерфейс, конверт-микроформат.

---

## llm/ — провайдер-слой

**У них.** Реестр API-семейств (`openai-completions`, `anthropic-messages`, …) с ленивой
регистрацией адаптеров поверх официальных SDK; **трёхслойные ретраи** (SDK-level; generic
`packages/retry` — экспонента + full-jitter, пол по Retry-After с корректным positive-jitter;
классификатор транзиентности `operation-retry.ts`: 500/502/503/504/DNS/timeout — да,
400/401/403/404 — нет); **настоящий фейловер моделей** (`runWithModelFallback`, цепочка
кандидатов, cooldown, запрет фейловера после первого видимого вывода; overflow уходит не в
фейловер, а в компакцию); каталог моделей в плагин-манифестах (contextWindow, цены, 30+
compat-флагов); prompt-caching первого класса (`CacheRetention`, `prompt_cache_key`,
cache-boundary маркер); **utility-модель** для лейблов/наррации (из манифеста провайдера);
auth-профили с ротацией и cooldown.

**У нас.** Порт `LLMClient` (2 метода) + 200-строчный клиент; порты эмбеддингов/реранкера с
локальными бэкендами; роутер и компактор — на основной модели. Без ретраев, usage, классификации,
фейловера, кэша.

**Мы лучше:** нет глобальных реестров (у них — `globalThis`-синглтоны, которые они сами
называют liability); площадь поверхности соответствует амбициям (~500 строк против десятков
тысяч compat-шимов); локальные эмбеддинги/реранкер за портами; минимальная сигнатура порта
(у них `StreamOptions` на 25 полей).

**Мы хуже:** ноль ретраев на всех слоях (транзиентный 429 убивает и прогон, и решение
роутера); нет usage/стоимости; нет фейловера (один endpoint упал = сервис стоит); нет
utility-модели; нет prompt-cache поддержки; нет first-token таймаута.

**Взять (всё ↔ hermes ↔ opencode — тройное подтверждение):**
1. `RetryingLLMClient`: backoff + jitter, пол по `Retry-After`, только транзиентные классы
   (их `operation-retry.ts` — самый компактный референс классификатора). ~150 строк.
2. Usage capture (`include_usage`) → `StreamFinished` → персист на сообщениях.
3. Utility-модель: второй `LLMConfig` (`OF_LLM_UTILITY_MODEL`) в роутер и компактор — чистый DI.
4. `FallbackLLMClient` (список клиентов; фейловер только до первого `TextDelta` — их правило
   `hasDirectlySentBlockReply`). Средняя стоимость, высокая ценность для хостеда.
5. `prompt_cache_key = dialog_id` + дисциплина stable-префикса (см. context).
6. First-token таймаут на `stream()` отдельно от общего.
**Сознательно не брать:** каталог из плагин-манифестов с 30 compat-флагами, OAuth-профили,
multi-SDK адаптеры — до второго семейства протоколов.

---

## memory/ — память и самообучение

**У них.** Файлы — источник истины (`MEMORY.md` + `memory/YYYY-MM-DD.md`), SQLite — производный
индекс (chunks + FTS5 + sqlite-vec + path-FTS + embedding-cache). **Проверено**: гибридный
поиск — вектор (cosine KNN) ∥ FTS5 BM25, мердж `0.7*vector + 0.3*keyword`, кандидаты 4×,
буст точных имён файлов, опциональные MMR (Jaccard, λ=0.7) и временной decay (half-life 30d);
версия эмбеддинг-модели в индексе со честным статусом «mismatched, reindex needed» и shadow-DB
публикацией; **pre-compaction flush** — скрытый ход при `window − reserve(20k) − soft(4k)`:
выгрести durable-факты в `memory/YYYY-MM-DD.md`, иначе `SILENT_REPLY_TOKEN`; **dreaming** —
recall-трекинг каждого `memory_search` → промоушн в долгосрочную при score≥0.75,
recallCount≥3, uniqueQueries≥2, recency half-life 14d (плюс REM-фазы и дневники — 200KB кода);
watcher на файлы. Вокруг: `active-memory` (проактивный recall перед каждым ответом сабагентом
с circuit-breaker'ом, инъекция в untrusted-тегах), `memory-wiki` (Obsidian-дамп),
`memory-lancedb` (альтернативный бэкенд).

**У нас.** Порт `MemoryStore` (user/global scope, upsert, LIKE, без эмбеддингов и
автоинъекции); инструкции с cosine + title-boost + cross-encoder реранком (usage_count
собирается, не используется); `history_search` только по текущему диалогу.

**Мы лучше:** мультитенантность как SQL-предикат (у них per-agent изоляция прикручена позже);
порты против глобального реестра плагинов + ~100-файлового менеджера; отсутствие целого класса
фейлов (нам не нужны shadow-publish/reindex-локи — стор авторитетен, не производен); реранкер
уже есть (у них — нет).

**Мы хуже:** нет семантического поиска по памяти (LIKE против гибрида; `memory/` игнорирует
существующий `EmbeddingClient`); нет lifecycle (recall-трекинг, промоушн, decay); нет
pre-compaction flush — durable-факты теряются при скролле хвоста; нет проактивного recall'а
(только по инициативе модели); нет версионирования эмбеддинг-индекса (смена модели молча
смешает векторы инструкций); brute-force cosine не масштабируется.

**Взять:**
1. **Гибридный поиск в `memory/`**: эмбеддинг-колонка (наш `EmbeddingClient`) + LIKE-ключевая
   нога, мердж 0.7/0.3, capability-порт `MemoryVectorSearch` по образцу
   `InstructionVectorSearch`. Их формула — drop-in замена `ranking.py` (там уже заложена).
   Высшая ценность.
2. **Pre-compaction flush**: при триггере компакции сначала тихий one-shot «извлеки durable
   факты → memory_store», потом суммаризация. Триггер-математика переносится прямо.
   (↔ hermes pre-compact harvest.)
3. **Версия эмбеддинг-модели** в инструкциях/памяти: колонка + «reindex needed» при рассинхроне,
   ленивое перевычисление при доступе (без shadow-DB).
4. **Recall-трекинг lite**: `recall_count`/`last_recalled_at` на `memories`; позже крон-job
   промоушна часто-вспоминаемого (их пороги: ≥3 recall, ≥2 уникальных запроса, half-life 14d).
   Dreaming-машинерию не брать.
5. Temporal decay множителем в ранжировании (off by default).
6. MMR на шортлист — когда результаты станут большими.
**Не брать:** файловое хранилище + watcher, memory-wiki, dreaming-нарративы, path-tier систему
(у нас короткие ключи — хватает exact-key буста).

---

## skills/ + instructions/ + datasets/ — скилы и знания

**У них.** Файловые скилы `SKILL.md` (frontmatter + body + scripts/references), 51 bundled;
источники: workspace/managed/personal/extraDirs/plugin, коллизии → первая побеждает, кэпы
(150 скилов/18k символов в промпте). **Проверено — прогрессивное раскрытие**: в системном
промпте только XML-каталог `<available_skills>` (имя, описание, location, `<version>` =
`sha256:` + 16 hex контента), промпт прямо инструктирует «прочитай SKILL.md когда задача
матчится; если version изменился — перечитай»; watcher бампит снапшот. Создание скилов —
только через **workshop**: `PROPOSAL.md` + секьюрити-скан + approve/reject/quarantine через
`before_tool_call` gate (70s таймаут = deny); авто-обучение (history-scan → черновики
предложений); curator архивирует stale. Хуки `before_tool_call` (rewrite/block/requireApproval),
`after_tool_call`, `tool_result_persist`. Plugin SDK: регистрация тулзов, хуков, каналов,
провайдеров, HTTP-роутов, RPC-методов.

**У нас.** Скилы за `Skill` Protocol, все спеки всегда в схеме; инструкции в БД (эмбеддинги +
реранк), видны только 300-символьными сниппетами поиска — **полного прочтения нет**;
`instruction_save` пишет сразу вживую, глобально; TOOL-инструкции через SSRF-guarded
executor. Без хуков, плагинов, фильтрации.

**Мы лучше:** структурированные валидированные вызовы (JSON-schema против их «markdown +
freestyle shell»); мультитенантность и per-user авторизация; egress-безопасность при
исполнении (у них скан контента, но исполнение — несандбоксенный shell); экономия архитектуры;
инструкции масштабируются поиском, а не кэпом промпта.

**Мы хуже:** нет прогрессивного раскрытия — все 17 схем в каждом промпте (↔ hermes);
**сохранённая инструкция нечитаема целиком** (сниппет 300 символов — сценарий, который агент
не может перечитать, почти сломан); `instruction_save` без черновика/подтверждения — и стор
глобальный, один юзер может отравить записи для всех; нет хуков (ни аудита, ни approval);
нет lifecycle.

**Взять:**
1. **Скил `instruction_get(title)`** — полная запись (сервис уже умеет `get_by_name`).
   Тривиально, высокая ценность: снимает потолок сниппета. (↔ hermes.)
2. **Порт `SkillHooks`** (before: args→args|Blocked|RequireApproval; after: result→result),
   вызов в лупе, цепочка из composition root. Фундамент для аудита и approval.
   (↔ opencode ↔ hermes.)
3. **Approval v1 для деструктивных скилов**: `cron_delete`, `data_forget`, `memory_delete`,
   перезапись `instruction_save` — через механизм pending-question в диалоге (их
   requireApproval + таймаут=deny). (↔ hermes draft+confirm.)
4. **Валидация TOOL-инструкций при сохранении**: парс spec + SSRF-проверка URL-шаблона на
   `instruction_save`, не только при исполнении.
5. Порт `SkillPolicy` `(spec, ctx) -> bool` — фильтрация по каналу/юзеру при сборке схемы.
6. Usage-буст в ранжировании инструкций.
**Не брать:** файловые скилы + watcher'ы, 41-эвентная шина хуков, plugin SDK — наши порты
дают те же точки расширения; два хук-шва покрывают ценные 20%.

---

## cron/ + tasks/ — планировщик и фон

**У них.** `CronJob` = spec + state; расписания `at` / `every`(interval+anchor) / `cron`(tz,
stagger) / `on-exit` (проверено в `types.ts`); payload'ы: systemEvent в главную сессию,
agentTurn (с override модели/тулзов/таймаута), raw command. Доставка: none/announce/webhook +
отдельный `failureDestination` и **failure-alert** (после N фейлов подряд, cooldown 1h).
Хранилище — SQLite, но загружается целиком в память; claim — process-local (single-gateway
допущение), **stuck-ремонт при старте** («interrupted by gateway restart», failed++).
Пропуски: catch-up при старте с лимитом и stagger'ом. **Ретрай с классификатором**
транзиентности (429/5xx/timeout) + guard `executionStarted` против повтора неидемпотентных;
3 попытки, backoff 30s/1m/5m. Статус исполнения и статус доставки трекаются раздельно.
**Изолированные сессии** для крона (`sessionTarget: isolated`) с announce-саммари в канал.
Леджер прогонов (`task-run-history`, кэп 2000/джоб), reaper сессий, watchdog таймаутов.
Тулз `cron` (status/list/get/add/update/remove/run/runs/wake — включая force-run и историю).
Общий task-registry со статусом `lost` (grace-period recovery осиротевших).

**У нас.** CAS-аренда одним UPDATE, asyncio-цикл, coalesce с счётчиком, ретрай с backoff,
one-shot, outcome-цепочка, нативные скилы с дедупом, per-job IANA TZ.

**Мы лучше:** мульти-инстансная безопасность claim'а (SQL CAS против process-local локов);
~700 строк за портами против 2800-строчного `timer.ts` + десятков файлов; coalesce с
счётчиком (у них — молчаливый одиночный повтор); мультитенантность в SQL; дедуп при создании.

**Мы хуже:** только cron-выражения (нет `at`/`every`); **слепой ретрай любых фейлов** (у них
классификатор — перманентные не ретраят); нет failure-алертов; нет раздельного статуса
доставки; нет изолированных сессий (крон загрязняет основной нарратив); нет истории прогонов
и ретенции; нет `cron_run`/`cron_update`; нет per-job таймаута; нет startup-ремонта (у них
есть, у нас — осиротение, в бэклоге).

**Взять:**
1. **Классификатор перед ретраем** (`cron/retry.py`, ~60 строк): транзиентные — ретрай,
   перманентные — нет. (↔ общая тема классификации ошибок.)
2. **Failure-алерты с cooldown**: `alert_after`/`last_alert_at`, системное уведомление после
   N фейлов подряд. Одна миграция, высокая ценность.
3. **Startup-ремонт**: осиротевшие PENDING/RUNNING → FAILED + `record_fire_result`.
   (↔ hermes ↔ opencode suspend/resume.)
4. **`cron_run` (force-fire) + `cron_update`** — дешёво, их `run/runs` активно используются
   для отладки.
5. История прогонов: индекс по `cron_job_id` на `tasks` + ретенция (keep last N) + скил
   `cron_runs`.
6. Расписания `at`/`every` (↔ hermes interval/timestamp — подтверждено дважды).
7. Per-job таймаут → отмена процесса через `TaskStore.cancel`.
8. **Изолированные fires с announce** — второй `CronWaker`, прогон в scratch-диалоге, в
   нарратив юзера только саммари. Отложить до реальных жалоб на загрязнение контекста.
**Не брать:** whole-store-in-memory, config-декларации, webhook-доставку, trigger-скрипты.

---

## web/ + telegram — каналы и поверхности

**У них.** Каналы — плагин-пакеты с манифестами и контрактом `ChannelPlugin` (~25 поверхностей:
config, pairing, security, groups, outbound, streaming, threading, auth, commands…), ленивая
регистрация. Сессии — иерархические ключи с peer/thread, identity-links между каналами;
`recordInboundSession` запоминает маршрут для крона. Стрим: общий single-flight
`createDraftStreamLoop` (коалесcing правок), чанкинг 200–800 с предпочтением границ абзацев;
Telegram: throttle 1s, **honor `retry_after`** (flood-wait suspension), аборт превью после 3
фейлов, min-dwell перед удалением. Авторизация: `allowFrom` allowlist + **pairing-коды**
(неизвестный отправитель получает код, владелец подтверждает out-of-band); группы с
mention-gating. Ack-реакции (👀 → удалить после ответа) и **статус-реакции** (emoji
стейт-машина: queued/thinking/tool/done/error/stall). Typing keepalive (3s, TTL 60s,
trip после 2 фейлов). Медиа (20MB, альбомы, дедуп). Gateway — WS JSON-RPC с
operator-скоупами; TUI — бэкенд поверх того же протокола; приложения на всех платформах.

**У нас.** FastAPI + SSE, trusted `X-User-Id`; telegram-адаптер (порт-клиент, правки черновика
с throttle, статус-строки, tag-safe чанкинг с UTF-16 багом, прогрев для крона, standalone).
Каналы — строки.

**Мы лучше:** пропорциональная простота (4-методный порт против ~25 поверхностей контракта);
мультиюзерность нативно (у них — allowlist'ы и identity-links поверх single-owner); один поток
событий — много рендереров; один composition root.

**Мы хуже:** нет авторизации вообще (бот открыт всем); typing — одиночный (протухает за 5с
при минутных прогонах); нет ack/статус-реакций; нет групп/тредов/топиков; нет медиа;
нет honor `retry_after` и политики чанков; UTF-16 баг; offset в памяти (апдейты за даунтайм
теряются); нет контрольной плоскости.

**Взять:**
1. **Allowlist + pairing** для Telegram (↔ hermes). Критично: сегодня бот открыт.
2. **Typing keepalive** в bridge (re-arm ~4с, TTL, trip после 2 фейлов). Часы.
3. Honor `retry_after` + политика чанков (мин. размер, границы абзацев) + UTF-16 фикс.
   (↔ hermes.)
4. Токен на HTTP API (↔ opencode).
5. Реакции-статусы вместо текстовых статус-строк — опционально, симпатичный UX.
6. Грамматика session-key (`channel:kind:peer[:thread]`) — когда появятся группы.
7. Персист последнего доставленного черновика для re-render после рестарта.
**Не брать:** WS-плоскость, device-keypair, мульти-аккаунт боты, медиа (пока).

---

## net/ + безопасность — сеть, секреты, аудит, MCP

**У них.** Exec-approvals: режимы deny/allowlist/ask/auto/full, запрос через gateway в
исходный канал с `/approve`, allow-always персистится (с привязкой к trust-path и посегментным
парсингом пайпов), safe-bins без промптов, опциональный LLM-ревьюер. Docker-сandbox
(network: none, readOnlyRoot, capDrop ALL — off by default). **net-policy**: блок special-use
IP (включая mapped/embedded IPv4, 6to4, teredo), cloud-metadata IP; и главное —
**пиннинг соединения к проверенным адресам** (`createPinnedLookup`, закрывает DNS-rebinding
TOCTOU), ручные редиректы с перевалидацией каждого хопа. Секреты: `SecretRef` (env|file|exec)
вместо plaintext, AES-256-GCM сентинелы, реестр для редактирования логов (включая
URL-encoded формы). **Аудит**: metadata-only события (псевдонимизация HMAC), SQLite, 30 дней,
писатель на выделенном треде с bounded-очередью. MCP: stdio/HTTP/OAuth/mTLS, неймспейсинг
`server__tool`, участие в общей tool-policy. Security-сканнер конфига + SAST rulepack.

**У нас.** `SsrfGuard` (resolve-every-IP, блок неглобальных, без редиректов) + whitelist-авторизация,
секреты структурно вне data-plane; без shell/file-туlzов, аудита, approval, MCP, redaction.

**Мы лучше:** поверхность атаки вычитанием (нет exec — вся их approval/sandbox машинерия
не нужна); секреты структурно вне логов и контекста (у них — best-effort реестр, ловящий
все формы каждого секрета); одна точка enforcement'а (у них политика размазана);
deny-by-default редиректы.

**Мы хуже:** **DNS-rebinding TOCTOU** — наш единственный реальный SSRF-пробел (валидация
резолва, потом httpx перерезолвит при коннекте; у них пиннинг); нет аудита (для хостед
мультиюзера — критично для форензики); нет redaction логов; нет явных тестов на
cloud-metadata/embedded-IPv4 (спасает `is_global` catch-all); деструктивные скилы без
подтверждения.

**Взять:**
1. **DNS-пиннинг**: резолв один раз в гварде, коннект на проверенный IP с сохранением
   Host/SNI (кастомный httpx transport). 1–2 дня, закрывает известную дыру.
2. **Аудит-лог metadata-only**: пакет `audit/` за портом `AuditStore`, события
   `skill.action.started/finished` (user_id, dialog_id, skill, статус) без args/result,
   asyncio-очередь в SQLite. (↔ opencode SkillHooks-аудит — один хук покрывает оба.)
3. **Bounded redaction-реестр**: регистрация известных секретов при старте, `redact()` в
   форматтере логов. ~100 строк.
4. Проверка при старте: значение, похожее на токен, не должно попадать в инструкции/память.
5. `RiskLevel` на `SkillSpec` + confirm-гейт для деструктивных (см. скилы).
6. MCP-клиент позже: порт, HTTP-only, неймспейс, каждый URL через `SsrfGuard`. Stdio —
   никогда (это host-exec другим именем). (↔ opencode.)
7. Их тест-фикстуры IP (cloud-metadata, embedded-IPv4) в тесты `SsrfGuard`.
**Не брать:** Docker-sandbox, safe-bin язык, LLM-ревьюер, opengrep.

---

## Фичи openclaw без аналога у нас вообще

- **Каналы как плагины** (~20 мессенджеров, нормализованная модель) — для нас: один
  Telegram-адаптер как образец; реестр не нужен до третьей поверхности.
- **Гибридный поиск памяти** (vector+FTS5 70/30, MMR, decay, версия индекса) — брать формулу,
  не файловую реализацию. Сильнейший кандидат для `memory/`.
- **Pre-compaction flush** и **dreaming/recall-промоушн** — брать flush и lite-версию
  трекинга.
- **Workshop (draft+approve скилов)** — брать approval-гейт на деструктивные операции.
- **Exec-approvals + Docker sandbox** — непереносимо (нет exec), кроме идеи confirm-гейта.
- **net-policy пиннинг** — брать, закрывает наш TOCTOU.
- **Canvas/talk/медиа/приложения** — вне нашего профиля.
- **Identity-links между каналами** — отложить до второго мессенджера.
- **Audit-лог** — брать (metadata-only).
- **Голос/STT/TTS** — позже, через порт (↔ hermes).

---

## Итог: приоритизированные рекомендации

(★★★ = подтверждено всеми тремя обзорами: hermes, opencode, openclaw.)

**Волна 1 — дёшево, критично:**
1. ★★★ Ретраи с классификатором транзиентности + пол по `Retry-After` (луп и LLM-клиент).
2. ★★★ Usage capture → токенный триггер компакции; overflow → компакция → один ретрай.
3. ★★★ Startup-recovery: осиротевшие задачи → FAILED + заметка «прервано рестартом».
4. ★★★ Идемпотентный submit по `client_message_id`.
5. Allowlist (+pairing) для Telegram; typing keepalive; UTF-16 фикс; honor `retry_after`.
6. ★★★ Убрать дату из системного промпта в конверт (prefix-cache); `prompt_cache_key`.
7. `instruction_get` — полное чтение инструкций.
8. Классификатор перед ретраем крона; failure-алерты с cooldown.
9. Bounded очереди подписчиков (↔ opencode); debounce+склейка входящих.
10. Redaction-реестр секретов в логах; их IP-фикстуры в тесты `SsrfGuard`.

**Волна 2 — надёжность и безопасность хостеда:**
11. ★★★ `SkillHooks` порт → аудит-лог metadata-only (один шов — обе цели).
12. ★★★ Utility-модель для роутера/компактора (DI).
13. ★★★ Гибридный поиск в memory/ (0.7/0.3, embedding-колонка) + версия эмбеддинг-модели.
14. Pre-compaction flush durable-фактов в память.
15. Approval-гейт для деструктивных скилов; валидация TOOL-инструкций при сохранении.
16. DNS-пиннинг в `SsrfGuard` (TOCTOU).
17. ★★ Токен-аутентификация на HTTP API (hashed); per-user лимит процессов.
18. `cron_run`/`cron_update`; история прогонов с ретенцией; расписания `at`/`every`.

**Волна 3 — интеллект и стратегия:**
19. ★★★ Rolling-merge саммари + структурный шаблон (заодно их flush-триггер математика).
20. Recall-трекинг памяти → промоушн часто-вспоминаемого (lite, их пороги).
21. `FallbackLLMClient` (фейловер до первого токена); aux для title-задач.
22. MCP-клиент за портом (HTTP-only, SSRF-guard на URL).
23. Изолированные крон-fires с announce; реакции-статусы в Telegram.
24. Ретенция архива (чистка покрытых саммариями сообщений старше N дней).

**Сознательно не берём:** файловые скилы и watcher'ы, plugin SDK и 25-поверхностный
контракт каналов, exec/sandbox/safe-bins, dreaming-машинерию, memory-wiki, JSONL-транскрипты,
whole-store-in-memory крон, webhook-доставку, WS-плоскость, device-auth, медиа/canvas/talk,
LLM-ревьюер approvals, каталог моделей с compat-флагами.
