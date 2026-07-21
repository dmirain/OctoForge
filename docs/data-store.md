# Хранение произвольных данных: датасеты (JSONB в основной СУБД)

> Согласовано после обсуждения «MongoDB vs JSONB». Выбран вариант JSONB в основной СУБД;
> MongoDB отклонён (второй компонент, раздвоение векторного поиска, нет tenant-изоляции).
> **Статус: реализовано** (этап C, `octoforge_core/datasets/`).

## Идея

Агентно-управляемое структурированное хранилище пользовательских данных (трекеры: еда,
вес, привычки). Пользователь: «храни, что я ем» → агент не находит подходящего датасета →
создаёт его и дальше пишет/читает по дескриптору.

## Модель

### `datasets` — дескриптор (4-й тип инструкции)

`id`, `owner_user_id` (**всегда per-user**, в отличие от глобальных knowledge/skill/tool),
`name`, `description`, `schema` (JSON: поля, типы, обязательность), `usage_notes`
(как писать/читать/агрегировать), `retention` (политика хранения), `embedding`,
`version`, `created_at`, `updated_at`; unique (`owner_user_id`, `name`).

### `dataset_records` — записи

`id`, `dataset_id` FK, `owner_user_id`, `payload` (JSON), `created_at`.
(Эмбеддинги записей — позже, как и планировалось.)

Валидация записи по `schema` из дескриптора (исполнитель, не LLM).

## Реализация (этап C)

- **Модуль `datasets/`** — обособленный, зеркалит `instructions/`: граница `api.py`
  (Protocol `DatasetService` + JSON-friendly DTO + порт хранилища `DatasetStore`),
  локальная реализация `LocalDatasetService` (cosine поверх инъектируемого стора,
  дефолт — `SqlAlchemyDatasetStore`), свой `store.py`/`models.py`/`ranking.py`
  (независим от instructions: своя cosine-функция и буст точного имени +2.0).
  Порт `EmbeddingClient` перенесён в `llm/embeddings.py` (общий для обоих модулей).
- **Формат схемы** (JSON): `{"fields": [{"name": "item", "type": "string",
  "required": true}, ...]}`; `required` опционален (default false). Типы:
  `string`, `integer` (bool не считается), `number` (int|float, bool не считается),
  `boolean`, `date`/`datetime` (ISO-строки). Лишние поля в записи разрешены
  (документ-хранилище). `validation.py`: `parse_schema`/`dump_schema`/`validate_record`.
- **Дескриптор эмбеддится** как `name + "\n" + description + "\n" + usage_notes`;
  поиск — brute-force cosine по дескрипторам owner'а + буст точного имени, top-k,
  тай-брейк по имени.
- **Запрос записей** (`query_records`): SQL-фильтр по диапазону `created_at` +
  `ORDER BY created_at DESC` + `LIMIT MAX_SCAN_ROWS` (1000), фильтр равенства
  `equals` — в памяти поверх скана, тип-чувствительный (`5 != "5"`, `True != 1`);
  итоговый `limit` применяется после фильтра. Трекеры малы, JSON-индексы не нужны.
- **Удаление** (`delete_dataset`): явный каскад DELETE записей (SQLite без
  PRAGMA foreign_keys), возвращает число удалённых записей.
- **Owner-изоляция** — на уровне SQL (`WHERE owner_user_id`) во всех операциях:
  чужой датасет неотличим от несуществующего.
- **Лимиты `data_query`** — из конфига: `OF_DATASETS_QUERY_DEFAULT_LIMIT` (50),
  `OF_DATASETS_QUERY_MAX_LIMIT` (200).

## Рантайм-тулы (пополнение узкого набора)

- `data_put(dataset, record, description?, schema?, usage_notes?, retention?)` —
  создать датасет при отсутствии (тогда `schema`+`description` обязательны) или
  записать в существующий; запись валидируется по схеме, нарушения возвращаются
  текстом (LLM может исправиться).
- `data_query(dataset, equals?, date_from?, date_to?, limit?)` — узкий DSL фильтров:
  равенство полей, диапазон дат (date-only = весь день UTC), limit. Агрегация для
  отчётов — LLM по выборке (трекеры малы); настоящие агрегаты — позже.
- `data_forget(dataset)` — удаление датасета каскадом с записями; ответ — счётчик.

## Как агент этим пользуется

- Дескриптор находится тем же `skills_search` (вектор) рядом со знаниями/скилами/тулами
  (merged-выдача по score, датасеты помечены `[dataset]`, сниппет — description + fields) →
  агент понимает, куда и как писать.
- Пример: «съел яблоко» → найден датасет `food_log` (или создан) → `data_put` с payload по схеме.
- «Отчёт за неделю» (крон, см. cron.md) → `data_query` по диапазону дат → LLM агрегирует выборку.

## Приватность и удаление

- Все записи per-user; «забудь всё про еду» → удаление датасета каскадом с записями.
- Доступ к чужим датасетам невозможен на уровне исполнителя (owner проверяется тулом, не LLM).

## Почему не MongoDB (из обсуждения)

Вторая СУБД = второй компонент (деплой, бэкапы); раздвоение векторного поиска
(pgvector vs Atlas); коллекции от LLM на лету без изоляции и констрейнтов; разрыв с
реляционным ядром (диалоги/задачи/крон). JSONB даёт ту же гибкость документа внутри
выбранного стека (SQLite JSON1 на standalone, Postgres JSONB на distributed).
