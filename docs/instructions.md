# Скилы и тулы: сценарии, встроенные функции и эндпоинты

> Целевая модель (согласована 2026-07-21). **Статус: дизайн, реализация — следующими
> итерациями**; список расхождений с текущим кодом — в разделе «Миграция с текущей
> модели». Отменяет прежнюю модель «knowledge/skill/tool в едином хранилище с рантайм-
> тулами, всегда висящими в контексте» и типизацию скилов BASIC/DYNAMIC.

Документ описывает: терминологию, прогрессивное раскрытие тулов через поиск сценариев,
устройство записи скила, системный реестр и пользовательские скилы, поиск, модульность
и безопасность.

## Терминология и типы сущностей

1. **Тул (tool)** — встроенная функция кода: `cron_create`, `memory_store`, `data_query`,
   `http_request` и т.п. У тула — ёмкое описание (1–2 предложения) и JSON-схема
   аргументов; методика его применения живёт не в описании тула, а в сценариях.
2. **Скил (skill)** — сценарий использования тулов: текстовая запись в сторе
   инструкций. Когда применять, шаги, подводные камни, правила взаимодействия
   («перед удалением крон-задачи уточни у пользователя, что именно удаляешь»).
3. **Эндпоинт (endpoint)** — запись внешнего HTTP API: метод, url-шаблон, схема
   параметров, тип авторизации. Исполняется тулом `external_call`.
4. **Знание (knowledge)** — факт, актуальный для всех пользователей («API X требует
   заголовок Y»). Не память: память — отдельный модуль `memory/`.

Маппинг со старой терминологией: «скил» (код, `Skill`/`SkillSpec`) → **тул**;
«инструкция типа tool» → **эндпоинт**; «инструкция типа skill» → **скил**;
`SkillOrigin.BASIC|DYNAMIC` упраздняется; 300-символьные сниппеты в поиске упраздняются —
сценарий возвращается целиком.

## Модель доступа: прогрессивное раскрытие тулов

Проблема, которую решаем: тулов стало много, агент путается (крон vs фоновые задачи),
а развёрнутые описания всех тулов раздувают каждый запрос. Решение — агент получает
тулы по требованию, вместе со сценариями их применения:

- Каждый процесс (форграунд, фоновый, крон-выстрел) стартует со схемой тулов из
  **одного** тула: `skills_search`.
- `skills_search(query, k?)` за один вызов возвращает: top-k скилов **целиком**,
  релевантные знания и эндпоинты — и **спеки тулов**, резолвнутые из `tool_keys`
  найденных скилов (см. ниже).
- Найденные тулы **активируются для процесса**: добавляются в его tool-schema на
  следующих итерациях. Набор живёт до конца процесса (повторный поиск докапливает
  его), в нарратив не персистится; новый процесс начинает заново с `skills_search`.
- Эндпоинты исполняются через `external_call` — он активируется, когда найден хотя бы
  один эндпоинт или скил, ссылающийся на него.

Итого типичный ход: сообщение пользователя → `skills_search` → сценарий + нужные тулы в
контексте → исполнение по сценарию. Одна поисковая итерация на задачу, ноль — когда
нужные тулы уже активированы в этом процессе.

## Запись скила

Поля записи: `type=skill`, `title`, `content` (текст сценария), `tags`,
**`tool_keys`** — список ключевых фраз для поиска тулов (напр.
`["cron create", "cron list", "cron delete"]`), `system` (флаг, см. ниже), версия и
счётчики `usage_count`/`success_count` как сейчас.

- Резолв `tool_keys` → тулы: поиск по реестру тулов (имя + описание) тем же
  ранжированием; ключи, не нашедшие тул, пропускаются с warning-полем в ответе поиска
  (сигнал устаревшего сценария).
- `instruction_save` получает параметр `tool_keys`; промпт обязывает заполнять его при
  сохранении сценария (см. «Промпт»).
- Полный текст сценария всегда доступен в выдаче поиска — отдельный тул «дочитать
  запись» не нужен.

## Переезд тулов по доменным модулям

Тулы покидают общий «чулан» и живут в модулях, которым принадлежат по смыслу:

- `cron_create/list/delete/pause/resume` → `cron/`
- `memory_store/search/delete` → `memory/`
- `data_put/query/forget` → `datasets/`
- `history_search` → `context/`
- `task_spawn`, `task_list` → `tasks/`
- `web_search` → `search/`
- `http_request`, `external_call` → `net/`
- `skills_search`, `instruction_save` → `instructions/`

Пакет `skills/` остаётся фреймворком: протокол тула, контекст вызова, реестр.
Composition root собирает реестр из доменных модулей.

## Системный реестр и пользовательские скилы

У каждой записи — признак `system` (системная/пользовательская).

- **Системный реестр** — декларативный список системных скилов (и их эндпоинтов),
  собираемый в composition root при сборке рантайма. Core поставляет реестр по
  умолчанию (крон-сценарий и базовые записи); установщик (web) добавляет прикладные
  пакеты (погода и т.п.) и специфичные тулы и может подменить core-записи — на свой
  страх и риск.
- **Синк при старте**: реестр подменяет системную часть базы — его записи upsert'ятся,
  системные записи, исчезнувшие из реестра, удаляются. Пользовательские записи
  (`system=false`) синк не трогает.
- **Защита**: `instruction_save`/`delete` над системными записями → отказ. Правка
  системных — только через реестр в коде установщика.
- **Пользовательские скилы** создаются агентом через `instruction_save` (с
  обязательным `tool_keys`) и позже через UI; правятся и удаляются свободно.

Миграций данных для скилов нет: единственный механизм доставки системных записей —
синк реестра при старте.

## Системные скилы core-реестра

По одному скилу на модуль, покрывающему его тулы. Назначение каждого — перенести
методику из системного промпта в искомые сценарии: «когда применять / шаги /
осторожности». Тексты сценариев — на английском (они идут в LLM). Деструктивные
операции всегда с правилом «покажи и подтверди у пользователя перед удалением».

### `cron_jobs` — отложенные и периодические задачи (cron/)

- tool_keys: `["cron create", "cron list", "cron pause", "cron resume", "cron delete"]`
- Черновик сценария:

```
Scenario: scheduled and recurring jobs (reminders, periodic reports).
Anything tied to a future time or a schedule belongs to cron — never to task_spawn.
1. Work out the cadence. Compose the cron expression yourself ('0 9 * * *' = daily
   09:00). Ask the user's IANA timezone when unknown; use 'UTC' when unclear.
2. One-time reminder: one_shot=true and a dated expression (minute hour day-of-month
   month *) with the nearest future occurrence, e.g. '30 15 21 7 *' for Jul 21 15:30.
   One-shot jobs delete themselves after firing.
3. Call cron_create with title, schedule, prompt and timezone. The prompt is the
   instruction you receive on every firing — make it self-contained.
4. Confirm to the user: title, schedule, timezone, next fire time.
Managing jobs: cron_list to inspect, cron_pause/cron_resume for temporary disabling.
Before cron_delete: show the job from cron_list and confirm the exact job with the user.
Creating a duplicate returns 'already exists' — do not retry, show the existing job.
```

### `background_tasks` — фоновая работа (tasks/)

- tool_keys: `["task spawn", "task list"]`
- Черновик сценария:

```
Scenario: run real work in the background (result needed once, when ready).
1. Call task_spawn with a self-contained prompt describing the work.
2. Confirm to the user that the task started, then continue the conversation —
   do not wait for the result.
3. When a system message reports the task finished, briefly relay the result.
Use task_list to check status of this conversation's background tasks.
Not for reminders or anything scheduled — that is cron_create. If spawning is
refused because of the process limit, tell the user instead of retrying in a loop.
```

### `user_memory` — память пользователя (memory/)

- tool_keys: `["memory store", "memory search", "memory delete"]`
- Черновик сценария:

```
Scenario: durable user facts and preferences (name, city, diet, goals and the like).
1. Save facts with memory_store, scope user. Use scope global only for facts shared
   by everyone, and with care.
2. Call memory_search before personal recommendations or when the answer may depend
   on what the user told you earlier.
3. Do not duplicate what lives in instructions (shared knowledge) or datasets
   (structured records). Memory is per-user and shared across the user's surfaces.
Delete with memory_delete only on the user's explicit request.
```

### `user_datasets` — структурированные данные пользователя (datasets/)

- tool_keys: `["data put", "data query", "data forget"]`
- Черновик сценария:

```
Scenario: remember and track structured data for the user (food, weight, habits...).
1. Find the dataset via skills_search; if none fits, create it implicitly with
   data_put by declaring a JSON schema for the record.
2. Write records with data_put; read and build reports with data_query (equality
   filters, date ranges, limit).
3. Delete data with data_forget — after confirming with the user what will be deleted.
Datasets are private to the user: never mix one user's data into another's answers.
```

### `history_lookup` — поиск по истории диалога (context/)

- tool_keys: `["history search"]`
- Черновик сценария:

```
Scenario: look up something discussed earlier in this conversation.
Your context holds compressed summaries of earlier topics; only the recent tail is
verbatim. If the user refers to something not covered by the summaries or the tail,
call history_search with the distinctive phrase instead of asking the user to repeat
it. Narrow with topic/date filters when the first search is too broad.
```

### `web_lookup` — актуальные факты из интернета (search/)

- tool_keys: `["web search"]`
- Черновик сценария:

```
Scenario: look up current events or facts you do not know.
Call web_search with a focused query; answer from the results and cite the source
links. If the results are thin or contradictory, say so instead of guessing.
```

### `external_http` — внешние HTTP-вызовы (net/)

- tool_keys: `["external call", "http request"]`
- Черновик сценария:

```
Scenario: call an external API.
1. Prefer a discovered endpoint: call external_call with the endpoint name and its
   declared params instead of hand-crafting requests.
2. Use http_request only for one-off calls not covered by any endpoint.
Outbound calls pass a security guard: public hosts only, no redirects. If a call is
refused, report the refusal honestly instead of retrying variations.
```

### `skill_authoring` — поиск и создание сценариев (instructions/)

- tool_keys: `["skills search", "instruction save"]`
- Черновик сценария:

```
Scenario: find and author skill scenarios.
1. Before a non-trivial task, and whenever the request may match a saved scenario
   or endpoint (weather, reports, reminders, user data), call skills_search with a
   subject-plus-action query and follow the scenarios you find.
2. After completing a novel multi-step task, save the working scenario with
   instruction_save (type skill): clear steps, and tool_keys naming every tool the
   scenario actually uses — activated tools come from these keys on future runs.
   Save durable facts as type knowledge.
3. Search before saving: update the existing scenario instead of creating a duplicate.
```

## Промпт

Системный промпт сокращается до мета-правил: формат ответов; «перед нетривиальной
задачей вызови `skills_search` и следуй найденным сценариям»; «после новой многошаговой
задачи сохрани сценарий через `instruction_save`, заполнив `tool_keys`». Пер-туловые
правила (крон vs `task_spawn`, работа с памятью, датасеты, `web_search`,
`history_search`) переезжают из промпта в системные сценарии соответствующих скилов.

## Оптимизация итераций

- Один вызов поиска = сценарии + спеки тулов (без второго round-trip за тулами).
- Полный текст сценария в ответе (без отдельного «дочитать»).
- Активированные тулы живут до конца процесса; повторные поиски докапливают набор.
- Роутер тулами не пользуется и не трогается.

## Поиск и эмбеддинги

- Embeddings endpoint (OpenAI-совместимый) или локальный бэкенд — как сейчас, за
  портом `EmbeddingClient`.
- Standalone: cosine brute-force по таблице (или sqlite-vec позже), буст точного
  title, опциональный cross-encoder реранк. Distributed: pgvector + tsvector
  (гибридный поиск), `usage_count`/`success_count` влияют на ранг.
- Полнота формулы (70/30 + MMR + decay) — бэклог исследований (plan.md, п. 9).

## Модульность: выделение в сервис, замена реализации

Хранение и поиск инструкций — самодостаточный пакет `core/instructions/` за единым
фасадом `InstructionService` (Protocol): `search` / `save` / `get_by_name` / `delete`.
Модуль только хранит, ищет и ранжирует; **исполнение — на стороне core**: эндпоинты
выполняет исполнитель `net/external.py`, тулы активирует луп.

- Петля и тулы зависят только от фасада; SQL, эмбеддинги и ранжирование — детали
  реализации внутри пакета.
- DTO фасада — JSON-совместимые объекты: границу можно поднять в HTTP без смены
  вызовов (выделенный сервис инструкций).
- Реализация подменяется в composition root: локальная (SQL + cosine) сейчас;
  pgvector/внешний поиск — позже. Слой хранения подменяется отдельно: порт
  `InstructionStore` (+ опциональный `InstructionVectorSearch`) инъектируется в
  `LocalInstructionService` — см. [modularity.md](modularity.md).
- Таблицы модуля — его собственность: вынос в сервис не тянет чужие таблицы.
- Конфиг модуля (top-k, embedding endpoint) приходит конструктором из composition
  root; белый список base-url — конфиг исполнителя, к модулю отношения не имеет.

## Безопасность

- `external_call` подставляет внутреннюю авторизацию только для белого списка base-url
  (composition root); произвольные эндпоинты её не получают. SSRF-гвард — на каждый
  вызов.
- Системные записи защищены от агента и UI (см. выше): пользовательский диалог не
  может испортить общие сценарии.
- Пользовательский скил, созданный агентом, глобален — учитывать при промптинге
  `instruction_save` (подтверждение человеком — отдельная итерация, см. plan.md).
- Доступ к датасетам — только владельцу (проверяет исполнитель, не LLM).

## Миграция с текущей модели (статус кода)

Что предстоит сделать, чтобы код соответствовал этому документу:

1. Переименования: тип инструкций `tool` → `endpoint` (с конвертацией записей);
   кодовые «skills» → «tools» на уровне имён и документации; `SkillOrigin` удалить.
2. Переезд тулов из `skills/basic/` по доменным модулям (маппинг выше).
3. Запись скила += `tool_keys` и флаг `system`; `instruction_save` += `tool_keys`;
   защита системных записей.
4. `instructions_search` → `skills_search`: ответ += полные сценарии и спеки
   резолвнутых тулов; активация тулов per-process в лупе вместо `registry.specs()`
   на каждом вызове.
5. `seed.py` → декларативный системный реестр (дефолт core + прикладные пакеты web)
   с синком при старте.
6. Промпт — сокращение до мета-правил; пер-туловые правила → системные сценарии.
