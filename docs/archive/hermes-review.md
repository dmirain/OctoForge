# Сравнительное исследование: hermes-agent vs OctoForge

> Глубокий разбор [hermes-agent](https://github.com/NousResearch/hermes-agent) (Nous Research)
> относительно OctoForge — по каждому нашему модулю. Зрелость, размер команды и популярность
> в расчёт не берутся: сравниваются только механики и архитектура. Исследование по коду
> (8 параллельных разведок + спот-проверка ключевых утверждений).

## Обзор: две разные философии

**hermes-agent** — «самоулучшающийся» single-user CLI-агент. Монолит: god-object `AIAgent`,
`run_agent.py` (6.6k строк), `agent/conversation_loop.py` (~5.8k), `hermes_state.py` (~7.8k),
`gateway/run.py` (~23k). Синхронный потоковый луп на тредах, глобальное состояние,
`Dict[str, Any]` на всех границах, конфиг читается из глубины стека. Хранилище — файлы
(markdown-память, JSON-крон) + один SQLite `state.db` с самолечением схемы. Живёт на машине
оператора: shell, файлы, браузер, approval-промпты в терминале.

**OctoForge** — многопользовательский хостед-агент. Хексагональная архитектура: порты-Protocol,
DI из composition root, asyncio-актор на диалог, типизированный поток событий, SQL с Alembic,
mypy strict. Нет shell/файловых тулзов сознательно.

Это не «большой против маленького»: у них механики, которых у нас нет и которые нам нужны;
у нас — архитектурная дисциплина, которой у них нет и которая им дорого обходится (сотни строк
самолечения JSON-файлов, три механизма claim'а в кроне, локи и heartbeat-хаки вокруг тредов).
Ниже — по модулям.

---

## agent/ + context/ — луп, контекст, компакция

**У них.** Синхронный `while`-луп с лимитом итераций и refundable-бюджетом
(`agent/iteration_budget.py`). Контекст трекается **реальными токенами** из `usage` ответа
(`agent/context_engine.py`): порог = % от окна модели; плюс грубая preflight-оценка (chars/4)
перед первым вызовом, плюс **реактивная компрессия** при ошибках провайдера (413,
context-overflow). Компрессия (`agent/context_compressor.py`) — трёхступенчатая:
1. дешёвый pre-pass без LLM: дедуп одинаковых tool-результатов по хэшу, замена старых
   результатов однострочниками (`[terminal] ran npm test -> exit 0`), обрезка args;
2. срез с защитой головы (system + первые сообщения) и хвоста, границы только между
   tool_call/tool_result группами, последний user и последний assistant — всегда в хвосте;
3. LLM-саммари по структурированному шаблону (Active Task дословно, Completed Actions,
   Key Decisions, Pending Questions, Remaining Work) с **итеративным обновлением**
   предыдущего саммари; анти-треш: счётчик неэффективных компрессий, cooldown при фейлах.
Есть ручной `/compress [focus]`. Промпт собирается тирaми (stable/context/volatile) с кэшем
для prefix-cache (`agent/system_prompt.py`). Вместо нашего роутера — детерминированные режимы:
очередь с дебаунсом, interrupt-и-redirect, `/steer` (инъекция в следующий tool-результат без
прерывания). Аналога LLM-роутера между процессами нет.

**У нас.** `AgentLoop.stream()` — асинхронный генератор типизированных `LoopEvent`; актор
`ConversationRunner` владеет нарративом и fg/bg-процессами; LLM-роутер (INJECT/START_NEW/
CANCEL/PROMOTE) с детерминированным фолбэком; трёхуровневый контекст (архив → саммари с
темами → горячий хвост `seq > max(seq_to)`), фоновая компакция `LlmContextCompactor` вне
горячего пути, безопасные границы среза, salvaged-пары, `history_search`.

**Мы лучше:** асинхронный типизированный поток событий против тредового монолита с
колбэками; нарратив вместо транскрипта (персистим только user/финалы/системные — нам не нужны
их `_sanitize_tool_pairs` и repair-машинерия); LLM-роутинг между процессами (у них только
прерывание или очередь); компакция вне горячего пути; порты на всё; состояние компакции
выводится из данных, а не из in-memory `_previous_summary`.

**Мы хуже:** нет учёта реальных токенов (chars/4, нет preflight, нет реактивной компрессии
при ошибке провайдера); нет компакции **внутри прогона** — ветка длинного tool-heavy
процесса растёт без bounds (прямо отложено в `docs/context.md` «Не входит»); нет гигиены
tool-результатов (дедуп/плейсхолдеры); саммари слабее (нет итеративного обновления,
структурных секций, бюджета длины); блок тем неограничен; нет cooldown при фейлах компакции.

**Взять:**
1. Учёт usage: добавить токены в `StreamFinished`/порты, порог = % окна модели; символы —
   как фолбэк. Средняя стоимость, высокая ценность — фундамент для всего ниже.
2. Mid-run компакция ветки: суммаризация старых итераций, срез только на границах итераций,
   tool-пары атомарны, последний user защищён. Их `_find_tail_cut_by_tokens` прямо портируется
   в порт `BranchCompactor`. Высокая ценность для длинных прогонов.
3. Дешёвый pre-pass tool-результатов в ветке (дедуп + однострочные плейсхолдеры, без LLM).
4. Итеративное обновление саммари + структурный шаблон (active task verbatim / decisions /
   pending asks) в `context/prompts.py`, с сохранением наших тегов тем.
5. Счётчик неэффективных компакций + cooldown при фейлах (in-memory, per dialog).
6. Реактивная компакция при context-overflow ошибке провайдера (классификация ошибок в лупе,
   компакция, один ретрай).
7. Ручной триггер компакции с focus-темой (скил или endpoint).
**Не брать:** ротацию сессий, `api_content`-сайдкары для byte-stable replay, тир-куны под
prefix-cache — артефакты монолита и персиста транскрипта, которых наша модель избегает.

---

## llm/ — LLM-слой

**У них.** Мультипровайдерность: декларативные `ProviderProfile` (`providers/base.py`) +
транспорты по `api_mode` (`agent/transports/`) для перевода форматов (chat_completions,
anthropic_messages, codex_responses, bedrock), нативные адаптеры Anthropic/Gemini/Vertex/
Bedrock/Azure. Но жизненный цикл клиента, ретраи и стрим живут в god-object — это
format-translation, не порты. Дальше по механикам: jittered backoff (`agent/retry_utils.py:36`)
+ **фейловер на fallback-цепочку провайдеров посреди хода**; prompt caching
(`agent/prompt_caching.py`: стратегия «system + 3 последних», 4 брейкпоинта, TTL 5m/1h);
rate-limit трекер по 12 заголовкам; метаданные моделей из models.dev (окно, цены);
**auxiliary client** (`agent/auxiliary_client.py`) — дешёвая модель для сайд-задач
(компрессия, title, session_search) с per-provider картой дешёвых моделей; watchdog стрима
(idle-timeout по чанкам, повышенные таймауты для reasoning-моделей); think-scrubber тегов.

**У нас.** Порт `LLMClient` (2 метода) + один адаптер `llm/openai.py` (~200 строк, ручной SSE,
аккумулятор tool_calls); порты `EmbeddingClient`/`RerankerClient` с локальными бэкендами.
Без ретраев, кэша, usage, rate-limit.

**Мы лучше:** порт настоящий (фейк для тестов вместо патчинга god-object), DI из одного места,
async-native стрим, доменные объекты на границе, локальные эмбеддинги/реранкер за теми же
портами.

**Мы хуже:** любой транзиентный 429/5xx/обрыв роняет ход; нет prompt caching (системный промпт
+ нарратив переплачиваются каждую итерацию каждого процесса); нет usage/стоимости; один
endpoint; роутер (вызывается на каждое сообщение!) и компактор работают на основной модели;
зависший SSE висит до общего таймаута — фоновые процессы умирают молча; нет scrubber'а
think-тегов.

**Взять:**
1. Декоратор `ResilientLLMClient(LLMClient)`: jittered backoff на 429/5xx/transport errors,
   attempts в конфиге. ~150 строк, максимальная цена/стоимость.
2. Второй инстанс клиента как «aux-модель» (`OF_AUX_MODEL`) — порты не меняются, чистый DI;
   в роутер и компактор. Дёшево, роутер зовётся на каждое сообщение.
3. Usage capture: `stream_options={"include_usage": true}`, токены в `StreamFinished`,
   персист. Фундамент для токенных триггеров компакции и учёта стоимости.
4. Stream idle watchdog: per-chunk `asyncio.timeout` в `stream()`, конфиг
   `OF_LLM_STREAM_IDLE_SECONDS`. ~20 строк, критично для unattended фоновых процессов.
5. Cache-брейкпоинты (system + граница блока тем) под флагом, когда endpoint это honour'ит
   (OpenRouter/Anthropic-wire). Наша структура «system + темы + хвост» идеально ложится на их
   «system_and_3».
6. Think-scrubber как stateful фильтр `TextDelta` между клиентом и лупом — когда реально
   появится reasoning-модель.
**Не брать сейчас:** models.dev (достаточно `context_window` в конфиге), failover-цепочки,
нативные адаптеры — до появления второго провайдера; тогда копировать их declarative
`ProviderProfile`-паттерн.

---

## memory/ + обучение — память и learning loop

**У них.** Два markdown-файла на профиль (`MEMORY.md`/`USER.md`) с char-бюджетами; один
`memory`-tool (add/replace/remove + атомарные batch-operations). Главное — **фоновый
review-форк**: после каждого хода демон-тред переигрывает диалог в форкнутом агенте с
whitelist'ом memory/skill тулзов и предписывающим промптом, записывая напрямую в сторы
(`agent/background_review.py`). Замороженный снапшот памяти инъектится в системный промпт на
сессию; записи внутри сессии идут на диск, но не в промпт (инвариант prefix-cache). Порты
внешних провайдеров памяти (`MemoryProvider` ABC с хуками `prefetch/on_pre_compress/...`),
Honcho — user-modeling. Curator: при простое — lifecycle-переходы скилов (stale→archive,
только архив, pinned защищён) + LLM-консолидация. Guardrails: скан инъекций при записи и при
загрузке снапшота, drift-детекция с `.bak`, опциональный human approval.

**У нас.** Порт `MemoryStore` (user/global scope, upsert, LIKE-поиск, без эмбеддингов),
скилы `memory_*`, правило 10 в промпте. Автоинъекция отложена. Обучение — только правила 7–8
промпта (сохрани инструкцию после новой задачи).

**Мы лучше:** архитектура (порты/DTO под HTTP-вынос против module-level функций и тредов);
мультиюзерность by design; семантический поиск по инструкциям (эмбеддинги + реранк) — у них
скилы адресуются только 60-символьным описанием в индексе; состояние в одной мигрируемой БД
против разрозненных файлов.

**Мы хуже:** нет автоинъекции памяти — recall зависит от того, что агент вспомнит вызвать
`memory_search`; нет learning loop (ни review-форка, ни curator'а, ни nudge'ей); нет
cross-dialog поиска (у них FTS5 BM25 + сниппеты + окна по всем сессиям); нет бюджетов и
консолидации памяти; нет извлечения фактов перед компакцией (их `on_pre_compress`); нет
threat-сканирования записей.

**Взять:**
1. Автоинъекция памяти в ветку: `list_for_user` в `MemoryStore`, бюджетный блок рядом с
   блоком тем при сборке ветки. Низкая стоимость, высокая ценность — делает правило 10
   реальным.
2. Pre-compaction harvest: промпт компактора дополнительно эмитит durable-факты →
   `MemoryStore`. Хвост скроллится — факты не теряются.
3. Фоновый review как **наш фоновый процесс**: по idle или после N сообщений — процесс с
   review-промптом и whitelist'ом скилов (`memory_store`, `instruction_save`). Ложится на
   акторскую модель без fork-машинерии; взять их `_COMBINED_REVIEW_PROMPT` со списком сигналов
   и секцией do-NOT-capture.
4. FTS5 по архиву + расширение `history_search` на все диалоги юзера (изоляция в SQL).
5. Usage-aware ранжирование инструкций (`usage_count` уже собирается).
6. Threat-scan контента, попадающего в промпт (предусловие для п.1).
**Отложить:** LLM-curator/консолидация, learning-graph UI, экосистема провайдеров памяти.

---

## instructions/ + datasets/ + skills/ — скилы и знания

**У них.** Файловые скиллы по стандарту agentskills.io: каталог с `SKILL.md` (YAML-frontmatter
+ длинный playbook) + resources/scripts. **Трёхуровневое прогрессивное раскрытие**: (1) индекс
«## Skills (mandatory)» запечён в системный промпт (имя + 60 символов, фильтры по
платформе/окружению/`requires_tools`, снапшот-кэш); (2) тулзы `skills_list`/`skill_view`
грузят полные тела по требованию; (3) `skill_view(name, file_path=...)` — отдельные
resource-файлы. Самоподдержка: правила промпта (сохрани скилл после сложной задачи, патчь
устаревшие немедленно), `skill_manage` (create/edit/patch/delete), тот же review-форк,
curator, hub с секьюрити-сканированием. Toolset'ы с runtime-gating через `check_fn`.

**У нас.** Скилы = код за `Skill` Protocol, все спеки всегда в схеме; инструкции в БД
(эмбеддинги + реранк, сидирование), исполнение внешних вызовов через `ExternalCallExecutor`
(SSRF-guard, whitelist); датасеты с JSON-схемой и owner-изоляцией (у них аналога нет вообще).

**Мы лучше:** порты/DI против глобальных кэшей и env-чтений из глубины; мультиюзерность в SQL;
структурированные датасеты с валидацией — уникально наше; безопасность динамических вызовов
(SSRF + whitelist) против выполнения произвольных shipped-скриптов; типизация.

**Мы хуже:** нет прогрессивного раскрытия — все спеки скилов в схеме всегда, инструкции видны
только как 300-символьные сниппеты поиска (агент должен *вспомнить* поискать; их обязательный
индекс + `skill_view` строго сильнее для recall'а); нет петли самоподдержки и lifecycle;
контент инструкций бесструктурный, нет patch-операции (только полная замена);
`external_call` слабый (URL-шаблоны, только строковые параметры, нет headers/body/response-
extraction); brute-force cosine по всей таблице в процессе.

**Взять:**
1. Двухуровневое раскрытие инструкций: компактный индекс (type + title + теги) в системный
   промпт через `PromptProvider` + скил `instruction_get(name)` с полным контентом.
2. Post-run review-процесс (см. раздел памяти).
3. Usage-aware blend в `instructions/ranking.py`.
4. Lifecycle инструкций: `archived`/`pinned`, stale-by-usage, периодическая консолидация
   фоновым процессом (наш cron). Инвариант «только архив, не удаление».
5. Конвенция структуры skill-инструкции (When to use / Steps / Pitfalls) с валидацией при
   `instruction_save` + `mode: patch` для точечных правок.
6. Богаче tool-spec: header/body-шаблоны, типизированные параметры, response-extraction
   (JSON pointer) — в рамках текущей SSRF/whitelist модели.
**Не брать:** hub/marketplace, inline-shell в скиллах, toolset-distributions (это для
генерации обучающих данных).

---

## tasks/ + net/ — фон, делегация, безопасность исполнения

**У них.** Субагенты (`tools/delegate_tool.py`, 3.6k строк): изоляция — свежий диалог без
истории родителя, свой system-промпт из goal+context, toolset родителя минус блок-лист;
передача контекста — две строки; результат — финал ребёнка с **бюджетом длины** от остатка
контекста родителя; параллельный batch на ThreadPoolExecutor с cap'ом и depth-cap'ом;
async-режим с durable-реестром в SQLite и **доставкой результата новым ходом когда idle**
(инвариант prefix-cache). Code execution (PTC): LLM пишет Python-скрипт, вызывающий тулзы по
RPC, в контекст возвращается только stdout — нулевая стоимость промежуточных шагов; whitelist
из 7 тулзов, лимиты. Окружения исполнения: `BaseEnvironment` ABC (local/Docker/SSH/
Singularity/Modal/Daytona) — та же форма, что наши порты. Guardrails: линтер shell-команд +
approval-стейтмашина (4k строк), `tool_guardrails.py` — **детектор петель** (повторные фейлы
с теми же аргументами, no-progress повторы; warn → опциональный hard stop), чекпоинты через
shadow-git перед каждой файловой мутацией, классификатор ошибок API.

**У нас.** Фоновые процессы актора через порт `TaskSpawner`, `OF_MAX_PROCESSES`, свёртка
результата в нарратив + репорт-прогон, exactly-once по `result_delivered`, outcome-цепочка
для крона. Сеть — только через `SsrfGuard` + whitelist-авторизация. Shell/файловых тулзов нет
сознательно.

**Мы лучше:** модель конкурентности (asyncio-актор против демон-тредов, TLS-колбэков и
обходов зависших воркеров — их `daemon_pool.py` это симптом); ограниченная типизированная
поверхность скилов; мультиюзерность (`SkillContext.user_id` → owner-изоляция в SQL);
сетевая безопасность чище (весь сетевой путь через SSRF-guard; их терминал может `curl`
что угодно).

**Мы хуже:** нет исполнения кода вообще (их PTC-паттерн «скрипт + RPC, назад только stdout» —
отличный механизм экономии контекста); нет workspace/файловых артефактов; делегация мельче
(нет batch-спавна, нет depth-модели — наш bg-процесс может `task_spawn`'ить безгранично,
ограничено только числом процессов; нет бюджета размера результата; нет durable-доставки);
нет детектора петель (только `max_iterations`); нет контентного threat-сканирования
web-результатов.

**Взять:**
1. **Детектор петель** — портировать `tool_guardrails.py` почти as-is: чистый контроллер,
   счётчики per-процесс, warn-then-block через tool-результат. ~150 строк, высокая ценность.
2. Бюджет результата таски: `OF_TASK_RESULT_MAX_CHARS` + усечение completion-заметки.
3. Depth-учёт спавна: поле глубины в `SkillContext`, отказ за `OF_MAX_SPAWN_DEPTH`. Закрывает
   реальную дыру runaway-стоимости.
4. Durable-регидрация задач при старте: их `async_delegations` (delivery_state/claims) —
   референс для нашего известного разрыва PENDING/RUNNING при рестарте: на boot'е помечать
   осиротевшие FAILED с системной заметкой.
5. `task_cancel` → отмена `LoopControl` процесса (их `interrupt_subagent`).
6. PTC за портом `CodeExecutor` — только если/когда нужно исполнение кода, и минимальный
   бэкенд — hardened Docker (не host shell: для многопользовательского хостеда их
   local-режим неприменим).
**Не брать:** approval-регексы, smart-approval LLM, tirith — это выживание host-shell'а в
single-user CLI; наш ответ — «нет host shell», а не 4k строк линтера.

---

## cron/ — планировщик

**У них.** JSON-файл под flock + отдельный SQLite-леджер попыток (`executions.db`:
claimed→running→completed/failed/unknown, liveness по pid+start-time). Тик раз в 60s на
треде. Виды расписаний: `once` (ISO или «30m»), `interval`, `cron` (croniter) — **одна
глобальная TZ**. Пропуски: grace = половина периода [120s, 2h], за пределами — coalesce и
fast-forward. Принципиально **at-most-once**: `next_run_at` двигается *до* исполнения.
**Ретраев нет** — фейл пишет `last_status=error` и ждёт следующего слота. Доставка результата
в платформы с фан-аутом, `[SILENT]`-протокол для молчаливых мониторов, durable outbox
(`delivery_ledger`). Создание: CLI + один жирный тулз `cronjob` (per-job модель/тулсет/
скрипт/`context_from`-чейнинг/workdir), скан инъекций в промптах. Blueprints/suggestions —
каталог шаблонов автоматизаций с consent-first предложениями. Порт `CronScheduler` ABC для
внешних движков.

**У нас.** Пакет `cron/`: порты `CronStore`/`CronWaker`/`Scheduler`, таблица с CAS-арендой
одним UPDATE, asyncio-цикл, coalesce пропущенных с счётчиком, **ретрай с backoff**,
one-shot, outcome-цепочка через `TaskOutcomeListener`, нативные скилы, дедуп при создании,
per-job IANA TZ.

**Мы лучше:** целостность хранения (типизированный ORM + Alembic против ~340 строк
самолечения кривых JSON-записей); claim одним SQL CAS против трёх наслоённых механизмов;
ретрай-политика (у них нет вообще); outcome-цепочка; per-job TZ (у них глобальная — для
мультиюзерного продукта неверно); ~720 типизированных строк против ~7.7k.

**Мы хуже:** только cron-выражения (one-shot требует синтеза dated-выражения агентом —
хрупко); нет repeat-N; нет аудита выстрелов («а сработало ли?» — только по логам); доставка
fire-and-forget в память — нет outbox'а при фейле отправки; нет `[SILENT]`-протокола;
нет heartbeat'а планировщика; нет сканирования промптов на инъекции; нет blueprint'ов.

**Взять:**
1. Виды расписаний `cron|interval|at` — обобщить `compute_next_fire`/`count_missed`, одна
   колонка. Убивает dated-cron хак для one-shot. ~1 день.
2. Леджер `cron_fires` (job_id, claimed_at, status, error) + recovery при старте. Отвечает
   на «сработало ли?» и готовит crash-recovery. 1–2 дня.
3. `[SILENT]`-протокол: финал крон-процесса, начинающийся с маркера (или пустой), не шлёт
   уведомление. Включает мониторы — их самый частый паттерн крона. Часы.
4. Delivery outbox (см. ниже, поверхности).
5. Repeat-N (`remaining`, декремент в `record_fire_result`, удаление на 0) — обобщает
   one_shot. Полдня.
6. Liveness планировщика: персист последнего тика, health-endpoint.
**Не брать:** JSON-хранилище, flock'и, тред-пулы, slot-form blueprint'ы (преждевременно),
regex-скан промптов (маргинально: наши промпты идут в LLM, не в shell).

---

## db/ + поиск по истории — персист

**У них.** Один монолитный SQLite (WAL) под синхронным `sqlite3` + лок, всё — dict'ы; две
FTS5-таблицы (unicode61 + trigram для CJK) с триггерами, санитайзер FTS-запросов, `snippet()`
с маркерами, BM25, фильтры, видимость (active/compacted/hidden); эволюция схемы —
string-diff `_reconcile_columns` + номер версии для дата-миграций, плюс тяжёлое самолечение
(бэкап/ремонт битой БД, rebuild FTS); retention (`prune_sessions` с фильтрами); ledgers
доставки и делегаций; крон — отдельный JSON. Времена — naive epoch floats.

**У нас.** SQLAlchemy async + Alembic, `UTCDateTime` форсит UTC на уровне типа, атомарный
`seq` подзапросом в INSERT, порты на все сторы, компакция недеструктивна (оригиналы на месте,
саммари указывают на диапазоны).

**Мы лучше:** архитектура (порты против god-module на dict'ах); дисциплина времени (UTC на
уровне колонок против epoch floats и local-time); миграции Alembic против string-diff
реконсилера (не умеет rename/drop/type-change, уже поймал баг и отгружает self-heal); крон в
БД с CAS; async под акторскую модель; недеструктивная компакция без пары флагов
active/compacted.

**Мы хуже:** нет FTS вообще (LIKE без индексов, без ранжирования, без фразовых запросов);
подача результатов беднее (нет сниппетов с маркерами, окна сообщений, anchored scroll);
нет retention/очистки вообще; нет startup-recovery (осиротевшие задачи и недоставленные
результаты теряются — признано в AGENTS.md); нет WAL/busy_timeout прагм и бэкапа перед
миграциями; нет персиста usage.

**Взять:**
1. FTS5 поверх `messages` за существующим портом `MessageArchive` (миграция + триггеры +
   backfill, `search_fts` с LIKE-фолбэком; санитайзер запросов взять у них).
2. Сниппеты + окно контекста в хитах (`messages_around(dialog_id, seq, window)`), расширить
   `history_search`.
3. Startup-recovery sweep (см. tasks): их state-machine леджера как шаблон.
4. SQLite-прагмы при connect (WAL, `busy_timeout`, `synchronous=NORMAL`) через event-listener
   в `db/engine.py`. Тривиально.
5. Бэкап файла БД перед `bootstrap_schema`-upgrade. Тривиально, дешёвая страховка.
6. Простой retention: `prune_messages(dialog_id, before)` + конфиг — когда размер БД станет
   реальной проблемой.
**Не брать:** multi-process write contention machinery, schema self-repair, lineage сессий.

---

## web/ + telegram — поверхности

**У них.** Один gateway-процесс на ~20 платформ: реестр с ленивыми импортами, общий ABC
`BasePlatformAdapter` (5.9k строк), нормализация в `MessageEvent`, единый async-пайплайн.
Стрим — платформенно-агностический консьюмер с edit-interval 0.8s, адаптацией к flood-control
(strike-счётчик, backoff, отключение правок), re-send финала новым сообщением. Чанкинг с
учётом **UTF-16 code units** (`utf16_len`, `gateway/platforms/base.py:141`) — лимит Telegram
считается в UTF-16. Auth: env-allowlist'ы + pairing-коды (8 символов, TTL, rate-limit,
lockout). Cross-platform continuity **нет** — платформа зашита в session key. Delivery ledger
с честным at-least-once («♻️ Recovered reply»), реестр мёртвых чатов с самолечением.
Транскрипция голосовых (нейтральный маркер при фейле — урок: verbose-маркеры отравляли
историю). TUI (React+Ink, JSON-RPC по stdio) и React-дашборд (20 страниц) поверх того же
ядра; OpenAI-совместимый HTTP «platform» как обычный адаптер.

**У нас.** FastAPI dialog API (SSE, trusted `X-User-Id`, без auth) + статичный чат-UI;
telegram-адаптер ~765 строк: порт `TelegramClient` (4 метода), long-poll, bridge-рендерер
событий runner'а с throttle-правками, markdown→Telegram-HTML с tag-safe срезом, прогрев
мостов из БД для крона, standalone-режим на общем composition root'е.

**Мы лучше:** радикально меньше при той же функциональности на одну поверхность (765 строк
против 9.4k telegram-плагина); один поток событий — два рендерера (web SSE и Telegram оба
читают `runner.subscribe()`); общий composition root для web и standalone; устойчивость
парсинга апдейтов (poison-update skipping, offset двигается всегда).

**Мы хуже:** нет auth вообще (любой нашедший бота говорит с ним); нет гарантий доставки
(in-memory offset, нет ledger'а, крон в удалённый чат ретраит вечно); **латентный баг**:
`split_html_safe` меряет `len()` в code points, Telegram — в UTF-16 units; сообщение с
астральными emoji/CJK-B может быть отвергнуто API; нет адаптации к flood-control
(`RetryAfter` не читается); нет typing-refresh; текст-only, DM-only.

**Взять:**
1. **Delivery ledger** — порт в core + таблица; checkpoint вокруг `TelegramBridge._deliver`;
   sweep при старте рядом с прогревом мостов; маркер «♻️» для ambiguous. 1–2 дня, закрывает
   признанный разрыв.
2. Allowlist для Telegram (`OF_TELEGRAM_ALLOWED_USERS`) — часы; pairing-коды — день, по их
   модели (TTL/rate-limit/lockout).
3. **UTF-16 фикс** — портировать `utf16_len` + binary-search prefix в `markdown.py`. Часы,
   реальный баг.
4. Flood-control backoff: извлекать `RetryAfter`, растить edit-интервал, strike-out.
5. Typing-refresh пока активен прогон. Тривиально.
6. Транскрипция голосовых — когда понадобится: STT-порт в core (по образцу
   `EmbeddingClient`), нейтральный маркер фейла.
**Не брать сейчас:** platform registry (channel-string + bridge — правильный размер до
третьей поверхности). Cross-platform continuity — у них тоже нет; общее ограничение, не gap.

---

## Фичи hermes без аналога у нас вообще

С оценкой полезности именно для OctoForge:

- **Learning loop** (review-форк + curator + nudge'и) — самая ценная концепция проекта.
  У нас есть все механики, чтобы сделать это чище (фоновые процессы + whitelist скилов +
  cron). Брать, см. раздел памяти.
- **Прогрессивное раскрытие скилов** (индекс в промпт + загрузка тел) — решает нашу дыру
  recall'а инструкций. Брать.
- **FTS5 session search** — брать за портом `MessageArchive`.
- **Delivery ledger** — брать, закрывает известный разрыв.
- **Prompt caching** — брать под флагом, наша структура промпта идеально ложится.
- **Aux-модель для сайд-задач** — брать, чистый DI.
- **Субагенты с изоляцией контекста** — наши фоновые процессы уже близко; брать бюджет
  результата и depth-учёт. Полная изоляция «ребёнок без истории родителя» — осмысленна как
  опция `task_spawn`, не как отдельная подсистема.
- **Окружения исполнения (Docker/SSH/Modal/Daytona)** и **PTC code execution** — только
  вместе с sandbox-бэкендом; host-shell вариант для нас неприменим. Долгосрочно интересно
  как порт `CodeExecutor`, сейчас — нет.
- **MCP, браузер, computer-use, TTS/STT, image/video gen** — точечно, по запросу; STT-порт
  первым кандидатом для Telegram.
- **TUI** — не нужен (наш интерфейс — web/Telegram).
- **User-modeling (Honcho-стиль)** — интересно поверх нашего `MemoryStore` позже: периодический
  диалектический процесс, пишущий профиль юзера в global-scope память.
- **Research-инструментарий** (batch trajectories, trajectory compression) — не наш профиль.

---

## Итог: приоритизированные рекомендации

**Волна 1 — дёшево, закрывает дыры (дни):**
1. `ResilientLLMClient` (ретраи с jitter) + stream idle watchdog.
2. UTF-16 фикс в telegram-чанкере — латентный баг.
3. Allowlist Telegram-пользователей.
4. `[SILENT]`-протокол для крона.
5. Расписания `interval`/`at` + repeat-N в кроне.
6. SQLite-прагмы (WAL/busy_timeout) + бэкап БД перед миграциями.
7. Depth-учёт и бюджет результата для `task_spawn`.
8. Usage capture (`include_usage`) — фундамент для компакции по токенам.
9. Детектор петель тулзов (порт `tool_guardrails.py`).

**Волна 2 — надёжность хостеда (неделя):**
10. Delivery ledger + startup-recovery sweep (осиротевшие задачи → FAILED, недоставленное →
    редоставка с маркером).
11. Леджер `cron_fires` + liveness планировщика.
12. Aux-модель для роутера и компактора (чистый DI).
13. Автоинъекция памяти в ветку (с бюджетом) + threat-scan контента + pre-compaction harvest.

**Волна 3 — интеллект (недели):**
14. FTS5 за `MessageArchive` + сниппеты/окна + cross-dialog scope в `history_search`.
15. Двухуровневое раскрытие инструкций (индекс в промпт + `instruction_get`).
16. Learning loop: post-run review-процесс (память + инструкции) — на наших механиках.
17. Mid-run компакция ветки + дешёвый pre-pass tool-результатов + итеративные саммари.
18. Prompt caching под флагом; usage-aware ранжирование инструкций; lifecycle инструкций.

**Сознательно не берём:** host-shell/file-тулы с approval-машинерией, hub/marketplace скилов,
platform registry до третьей поверхности, failover-цепочки провайдеров и models.dev до второго
провайдера, JSON/flock-хранилища, blueprint-формы, regex-скан cron-промптов, research-стек.
