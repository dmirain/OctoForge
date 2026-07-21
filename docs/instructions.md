# Скилы и тулы: сценарии, встроенные функции и эндпоинты

> Целевая модель v3 (согласована 2026-07-21). **Статус: реализовано** (кроме внутреннего
> rename Skill→Tool и канонической формы запроса intent/entity — обе отложены; поиск —
> свободный текст `skills_search(query, k?)`). Пре-поиск роутера **отменён решением от
> 2026-07-21** — признан избыточным усложнением, достаточно `skills_search` + правила
> промпта (см. раздел «Пре-поиск скилов до прогона»); код пока на месте.
> Отменяет прежнюю модель «knowledge/skill/tool в едином хранилище» и промежуточную v2
> «в схеме один skills_search, тулы активируются поиском».

Документ описывает: терминологию, модель доступа к тулам и скиллам, устройство записи
скила, системный реестр, поиск, модульность, безопасность и оценку подхода на фоне
конкурентов.

## Терминология и типы сущностей

1. **Тул (tool)** — встроенная функция кода: `cron_create`, `memory_store`, `data_query`,
   `http_request` и т.п. У тула — ёмкое описание (как называется, что делает, какие
   аргументы принимает); методика его применения живёт не в описании тула, а в сценариях.
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

## Модель доступа: все тулы в схеме, скилы — поиском

- **Все тулы всегда в tool-calling схеме** каждого процесса (форграунд, фон, крон):
  состав статичен и не меняется в рантайме. Описания — ёмкие (1–2 предложения +
  JSON-схема аргументов).
- **Скилов в контексте изначально нет.** Агент не должен импровизировать применение
  тулов: сначала `skills_search`, найденный скил попадает в контекст (как результат
  вызова) и остаётся до конца процесса — он и говорит, как правильно использовать тулы.
- **Поиск — на каждый интент сообщения пользователя.** Составная просьба («напомни
  вечером про продукты и скажи, что там с погодой») — это два интента: два поиска или
  один поиск с двумя формулировками. Повторный поиск по тому же интенту внутри процесса
  не нужен — скил уже в контексте.
- Запрос формируется **канонически**, а не сырым текстом пользователя:
  `skills_search(intent, entity, text?)` — нормализованное действие (remind / schedule /
  report / track / lookup / save / call-api…) + тип сущности (reminder,
  recurring-report, user-data, weather, history, web-fact…). Пример: «напиши мне
  вечером, что надо купить продукты» → `intent=remind, entity=reminder`
  («напомни про событие»). Сервис собирает эмбеддинг-запрос из канонических полей.

Итого типичный ход: сообщение → роутер (только маршрутизация) → агент вызывает
`skills_search` на каждый интент → сценарий в контексте → исполнение тулами по
сценариям.

## Запись скила

Поля записи: `type=skill`, `title`, `content` (текст сценария), `tags`, `system`
(флаг, см. ниже), версия и счётчики `usage_count`/`success_count` как сейчас.
Сценарий ссылается на тулы по именам в тексте; отдельного поля связей нет — схема
тулов статична, резолв не нужен. Полный текст сценария всегда доступен в выдаче
поиска — отдельный тул «дочитать запись» не нужен.

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
- **Пользовательские скилы** создаются агентом через `instruction_save` и позже через
  UI; правятся и удаляются свободно.

Миграций данных для скилов нет: единственный механизм доставки системных записей —
синк реестра при старте.

## Системные скилы core-реестра

По одному скилу на модуль, покрывающему его тулы. Назначение каждого — перенести
методику из системного промпта в искомые сценарии: «когда применять / шаги /
осторожности». Тексты сценариев — на английском (они идут в LLM). Деструктивные
операции всегда с правилом «покажи и подтверди у пользователя перед удалением».

### `cron_jobs` — отложенные и периодические задачи (cron/)

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

```
Scenario: look up something discussed earlier in this conversation.
Your context holds compressed summaries of earlier topics; only the recent tail is
verbatim. If the user refers to something not covered by the summaries or the tail,
call history_search with the distinctive phrase instead of asking the user to repeat
it. Narrow with topic/date filters when the first search is too broad.
```

### `web_lookup` — актуальные факты из интернета (search/)

```
Scenario: look up current events or facts you do not know.
Call web_search with a focused query; answer from the results and cite the source
links. If the results are thin or contradictory, say so instead of guessing.
```

### `external_http` — внешние HTTP-вызовы (net/)

```
Scenario: call an external API.
1. Prefer a discovered endpoint: call external_call with the endpoint name and its
   declared params instead of hand-crafting requests.
2. Use http_request only for one-off calls not covered by any endpoint.
Outbound calls pass a security guard: public hosts only, no redirects. If a call is
refused, report the refusal honestly instead of retrying variations.
```

### `skill_authoring` — поиск и создание сценариев (instructions/)

```
Scenario: find and author skill scenarios.
1. For every intent in the user's message call skills_search with the canonical form:
   the normalized intent (remind, schedule, report, track, lookup, save, call-api)
   and the entity type (reminder, recurring-report, user-data, weather, history,
   web-fact); add free text only when it narrows the search. Do not improvise tool
   usage before searching — the scenario says how to use the tools correctly.
2. After completing a novel multi-step task, save the working scenario with
   instruction_save (type skill): clear steps, naming every tool the scenario uses.
   Save durable facts as type knowledge.
3. Search before saving: update the existing scenario instead of creating a duplicate.
```

## Промпт

Системный промпт держит только мета-правила: формат ответов; «не импровизируй
применение тулов — следуй сценариям из контекста, а для любого непокрытого интента
сначала вызови `skills_search`»; каноническая форма запроса (intent + entity)
с примером маппинга; «после новой многошаговой задачи сохрани сценарий через
`instruction_save`». Пер-туловые правила (крон vs `task_spawn`, работа с памятью,
датасеты, `web_search`, `history_search`) живут в системных сценариях (раздел выше),
не в промпте. Нормализация интентов — на модели при вызове `skills_search` (форма
описана в системном скиле `skill_authoring`); роутер нормализацией не занимается.

## Оптимизация итераций

- Статичная схема тулов — стабильный prefix-cache: спеки не меняются ни между
  процессами, ни внутри процесса.
- Один поиск может покрывать несколько интентов (несколько формулировок за вызов).
- Полный текст сценария в ответе поиска — отдельного «дочитать» не требуется.
- Повторный поиск по тому же интенту внутри процесса не нужен: скил уже в контексте.
- Роутер тулами не пользуется и поиском не занимается (пре-поиск отменён, см.
  следующий раздел).

## Пре-поиск скилов до прогона

**Статус: УДАЛЕНО (2026-07-21).** Поле `searches` из контракта, tool spec и
промпта роутера, порт `PresearchPort`, `InstructionPresearch`
(`instructions/presearch.py`), инъекция заметки в ветку процесса в акторе,
`build_presearch` и `RunnerOptions.presearch` в composition root — всё убрано.
Пре-поиск признан избыточным усложнением:

- дублирование: одни и те же сценарии приходят и заметкой пре-поиска, и tool-результатом
  `skills_search`, а потом ещё и остаются в горячем хвосте;
- переменная заметка на второй позиции ветки ломает стабильный префикс
  («промпт + темы») для prefix-cache;
- цена удаления умеренная: одна лишняя итерация петли на непокрытый интент — дешёвая
  при prefix-кэшировании;
- вместо механической страховки — правило промпта «на каждый интент — `skills_search`»
  и системный скил `skill_authoring`. Осознанный риск: слабая модель может начать
  импровизировать тулами, не поискав сценарий; при подтверждении риска — дешёвый
  запасной выход (микрокаталог заголовков, см. «Где осознанно расходимся»), а не
  возврат пре-поиска.

Исходный замысел (историческая справка): чтобы основной LLM стартовал уже с
релевантными сценариями в контексте (и не тратил итерацию на поиск), поиск выполнялся
**до** прогона, бесплатно относительно LLM-вызовов: роутер возвращал кроме ops-пакета
список поисковых запросов `searches`, актор исполнял их через
`InstructionService.search` и подкладывал найденные скилы в ветку процесса
system-заметкой.

## Оценка подхода: плюсы, минусы, конкуренты

### Как делают конкуренты (проверено по коду)

**hermes — обязательный каталог в промпте + тул-догрузчик.**
В системном промпте всегда блок «## Skills (mandatory)»: плоский индекс
`имя: описание (≤60 символов)` с жёсткой инструкцией «просканируй список; при малейшей
релевантности ОБЯЗАН загрузить скил через `skill_view` — лучше лишний контекст, чем
пропущенный сценарий» (`agent/prompt_builder.py:1726`). Тело скилла догружается тулом
`skill_view(name)` (`tools/skills_tool.py:961`), вспомогательные файлы —
`skill_view(name, file_path=...)`. Встроенные тулы всегда в схеме (фильтрация
toolset'ами). Экономия: ~60 символов на скил в промпте вместо сотен строк тела.

**openclaw — каталог с версией-хэшем + универсальный read.**
В системном промпте всегда `<available_skills>`: name/description/location/**version**
(`src/skills/loading/skill-contract.ts:38-64`), version = `sha256:` + 16 hex контента.
Инструкция: «при матче задачи с описанием — прочитай файл скила; если version сменилась —
перечитай». Догрузка — универсальным тулом `read` (он и так всегда есть), специального
догрузчика нет. Watcher обновляет каталог между ходами; кэпы 150 скилов/18000 символов.

**opencode — каталог в инструкциях + тул `skill`.**
`<available_skills>` (id/name/description) рендерится в блок инструкций
(`core/src/skill/instructions.ts`). Догрузка — тул `skill(id)` (`core/src/tool/skill.ts`):
принимает только id из каталога, permission-check, возвращает тело + базовую директорию
+ список файлов скилла. Встроенные тулы всегда в схеме (materialize по permission).
Отдельно существует CodeMode — схлопывание большинства тулзов в один мета-`execute`:
альтернативный путь экономии схем, для нас преждевременный (песочница JS на тенанта).

**Общее у всех троих:** встроенные тулы ВСЕГДА в схеме; в промпте ВСЕГДА виден каталог
сценариев (имя+описание); лениво грузятся только тела сценариев.

### Где мы сошлись с конкурентами

Тулы всегда в схеме — как у всех троих; ёмкие описания тулов; методика применения —
в сценариях, а не в описаниях тулов; тела сценариев подгружаются по требованию.

### Где осознанно расходимся

Discovery скилов — **поиском, а не всегда-видимым каталогом**. Плюс: промпт не растёт
с числом скилов вообще (у openclaw каталог ограничен кэпом 150/18000 — у нас такого
потолка нет). Минус: recall-риск выше — агент должен поискать, чтобы узнать о скиле.
Компенсации: правило «поиск на каждый интент» (жёстче, чем инструкции конкурентов),
каноническая форма запроса (intent+entity), а если риск подтвердится на практике —
дешёвый запасной выход: микрокаталог заголовков системных скилов (~10 строк) в промпте.

### Плюсы v3 против отменённой v2 (активация тулов поиском)

- Статичная схема = стабильный prefix-cache: v2 била бы кэш при каждом добавлении
  тулов в схему посреди процесса.
- Грациозная деградация: при промахе поиска тулы всё равно видны и вызываемы.
- Нет per-process состояния и резолва `tool_keys` — проще луп и стор.

### Минусы и риски v3

- Схема растёт линейно с числом тулов → дисциплина ёмких описаний; при десятках тулов
  понадобятся гейты (у конкурентов — toolset'ы/permission, нам пока преждевременно).
- Путаница между похожими тулами гасится сценарием, а не отсутствием тула в схеме —
  зависим от того, что агент поискал и нашёл нужный скил.
- Поиск на интент — дополнительные итерации на составных запросах.

**Вердикт:** v3 — ближайший к конкурентам и простейший из рассмотренных; остающийся
риск (discovery без каталога) осознан, компенсирован правилом «на каждый интент» и
имеет дешёвый запасной выход.

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
выполняет исполнитель `net/external.py`.

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

Выполнено (пункты 1–7, кроме двух отложенных частей пункта 1 и канонической формы
пункта 4):

1. Переименования: тип инструкций `tool` → `endpoint` (с конвертацией записей);
   кодовые «skills» → «tools» на уровне имён и документации; `SkillOrigin` удалить.
2. Переезд тулов из `skills/basic/` по доменным модулям (маппинг выше).
3. Запись скила += флаг `system`; защита системных записей в `instruction_save`/`delete`.
4. `instructions_search` → `skills_search(intent, entity, text?)`: канонические поля
   запроса, ответ с полными сценариями.
5. `seed.py` → декларативный системный реестр (дефолт core + прикладные пакеты web)
   с синком при старте; реестр core = 8 системных скилов из раздела выше.
6. Промпт — сокращение до мета-правил; пер-туловые правила → системные сценарии.
7. ~~Пре-поиск: роутер возвращает `searches` рядом с ops-пакетом (`agent/router.py` +
   роутерный промпт), актор исполняет поиски и инъектит скилы в ветку (`agent/runner.py`,
   дедуп + бюджет ≤3).~~ Реализовано, но отменено решением от 2026-07-21 (см. раздел
   «Пре-поиск скилов до прогона») — подлежит удалению.
