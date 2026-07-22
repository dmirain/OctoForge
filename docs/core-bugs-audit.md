# Аудит багов `core/` (снимок 2026-07-22)

> Точечный код-ревью `octoforge-core` (все файлы `core/src`), в отличие от
> [quality-audit.md](quality-audit.md) — не архитектурный обзор, а список конкретных
> багов с подтверждённым местом и сценарием воспроизведения. Ничего не исправлено —
> это снимок находок для последующей приоритизации. Связанные доки:
> [quality-audit.md](quality-audit.md) (S6 — общая заметка про SSRF/`self_base_url`,
> уточняется находкой №1 ниже), [process-model.md](process-model.md), [cron.md](cron.md).

## 1. Обход SSRF-гарда через allowlist (критично, эксплуатируется) — `net/guard.py:64`

```python
if any(url.startswith(prefix) for prefix in self._allowed_prefixes):
    return
```

`web/src/octoforge_web/main.py:132` строит единственный `SsrfGuard` для агентского
скилла `http_request` с `allowed_prefixes=(settings.self_base_url,)` (по умолчанию
`http://127.0.0.1:8000`). Проверка — сырое сравнение префикса строки, поэтому URL вида
`http://127.0.0.1:8000@169.254.169.254/latest/meta-data/` проходит `startswith(prefix)`
(userinfo-сегмент `@` делает строку совпадающей с префиксом), а `urlsplit` резолвит
реальный хост в `169.254.169.254` — адрес облачных метаданных. Проверка резолва/IP
целиком пропускается — любой LLM-вызов `http_request` получает полноценный SSRF во
внутренние сервисы/метаданные облака.

**Фикс:** сравнивать распарсенные scheme+host(+port) с allowlist, а не сырой префикс
строки.

## 2. Частичный ответ теряется при отмене хода с незавершённым tool-call — `agent/loop.py:338-353` + `agent/runner.py:646-654`

`_interrupt_iteration` добавляет в branch сначала частичное `ASSISTANT`-сообщение, а
затем `TOOL`-реплики (плейсхолдеры отменённых вызовов):

```python
history.append(message)                                   # ASSISTANT (частичный контент)
history.extend(tracker.tool_messages(message.tool_calls))  # TOOL-реплики после
```

`_salvage_interrupted_turn` в runner-е спасает контент только если *последнее*
сообщение branch — `ASSISTANT`:

```python
last = process.branch[-1] if process.branch else None
if last is None or last.role is not MessageRole.ASSISTANT or not last.content:
    return
```

Сценарий: пользователь отменяет ход, пока ассистент уже застримил часть текста и
одновременно запросил tool-call, который ещё не резолвился. Branch заканчивается на
`TOOL`, а не `ASSISTANT` → проверка спасения не срабатывает, уже показанный
пользователю через `TextDelta` текст не попадает в нарратив и теряется — что
противоречит собственному назначению модуля («сохранить частичный ответ отменённого
хода, пометив как неполный»).

**Fix:** искать последнее `ASSISTANT`-сообщение, а не только `branch[-1]`, либо
спасать контент до добавления tool-реплик.

## 3. Крон-задача на паузе всё равно может сработать — `cron/store.py:112-135`

`list_due()` фильтрует по `CronJobRow.enabled`, но CAS-апдейт в `claim()` — нет:

```python
async def claim(self, job_id, expected_next_fire_at, owner, now, stale_before):
    statement = (
        update(CronJobRow)
        .where(
            CronJobRow.id == job_id,
            CronJobRow.next_fire_at == expected_next_fire_at,
            _claimable_clause(stale_before),   # enabled не проверяется
        )
        ...
    )
```

Сценарий: тик планировщика получает из `list_due` включённую задачу, у которой
подошло время; пользователь вызывает `cron_pause` до того, как отработает `claim()`.
`claim()` всё равно матчит (id + `next_fire_at` + claimable) и срабатывает — задача на
паузе всё равно фактически выполняется, что противоречит контракту скилла паузы
(«остаётся в списке, но не срабатывает»).

**Fix:** добавить `CronJobRow.enabled` в WHERE `claim()`.

## 4. `PROMOTE` необоснованно отклоняется на пределе лимита процессов — `agent/runner.py:407-424`

```python
def _exceeds_limit(self, cancelled: set[str]) -> bool:
    return len(self._processes) - len(cancelled) + 1 > self._max_processes
```

`_apply_promote` использует ту же проверку ёмкости, что и создание *нового* процесса,
хотя промоушен лишь переключает foreground среди уже существующих процессов и не
добавляет ни одного в `self._processes`. Когда диалог уже на `max_processes`,
`_exceeds_limit` всегда возвращает `True` (целевой процесс уже учтён в
`len(self._processes)`) — и «верни задачу X на передний план» отклоняется именно
тогда, когда это нужнее всего, хотя переключение не потребляет дополнительную ёмкость.

**Fix:** не применять `_exceeds_limit` в `_apply_promote` — лимит ёмкости относится
только к `START_NEW`.

## 5. `spawn_task`/`wake` мутируют состояние процессов в обход сериализации актора — `agent/runner.py:229-250`

Все прочие мутации `self._processes` идут через inbox актора (`_dispatch`,
однопоточно сериализован по построению). `spawn_task` (вызывается напрямую из скилла
`task_spawn`, который сам выполняется внутри pump-задачи процесса) и `wake`
(вызывается напрямую из `ConversationManager.wake`) вместо этого сразу зовут
`_spawn_process_task`. Тот метод проверяет `len(self._processes) >= self._max_processes`,
а затем несколько раз awaitит (`tasks.add`, `tasks.mark_running`) до того, как
наконец вызвать `self._create_process`. Два конкурентных вызова (например, фоновая
задача спавнит другую задачу, пока актор независимо стартует новый foreground-процесс
через `_apply_start_new`) могут оба пройти проверку ёмкости до того, как хоть один
создаст процесс — лимит `OF_MAX_PROCESSES` превышается.

**Fix:** провести `spawn_task`/`wake` через тот же inbox-сериализованный путь команд,
либо явный лок вокруг последовательности check-then-create.

## 6. Дублирующиеся глобальные записи памяти при конкурентной записи — `memory/store.py:32-57`

Докстринг модуля сам объясняет, что уникальность `(user_id, key)` для глобальных
записей (`user_id IS NULL`) не гарантируется констрейнтом БД (NULL никогда не
совпадает с NULL), поэтому уникальность обеспечивает сам store через find-then-insert.
Эта операция не атомарна: два конкурентных `memory_store(scope="global",
key="daily_tip")` (например, от двух разных пользователей) могут оба не увидеть
существующую строку и оба вставить — получаются две строки с одинаковым ключом.
`get` (`.first()` без сортировки) и `delete` после этого ведут себя недетерминированно
относительно дублей.

**Fix:** обернуть в транзакцию с блокировкой строки, либо завести partial unique
index / сентинел-значение для глобальной области вместо опоры на `NULL`.

## 7. `date_to` трактуется как исключающая граница для полного datetime вопреки описанию — `context/tools.py:35-38,114-134`, `context/store.py:165`

Схема тула `history_search`, которую видит агент, говорит, что `date_to` —
«inclusive upper bound» для «ISO date/datetime». Внутренний контракт
(`context/api.py:122`) и store (`created_at < restriction.date_to`) трактуют её как
исключающую. `_parse_date` компенсирует это только для голой даты `YYYY-MM-DD`
(сдвигает на следующую полночь); полный ISO-datetime компенсации не получает —
сообщение, созданное ровно в момент `date_to`, молча выпадает из выдачи, что
противоречит тому, что агенту сказано про параметр.

**Fix:** либо компенсировать и полные datetime (сдвиг на 1мкс / `<=`), либо
поправить описание тула на «exclusive».

## 8. Опциональный шаблонный URL-параметр даёт сырой `KeyError` вместо чистой ошибки — `net/tool_spec.py:113-116`, `net/external.py:104-118`

`_validate_template_fields` проверяет только, что `{placeholder}` в `url_template`
*объявлен* в `params_schema`, но не то, что он `required: true`. `_validate_params`
поэтому не считает пропущенным опциональное поле, входящее в шаблон, а
`_render_url`'s `spec.url_template.format(**quoted)` кидает необработанный
`KeyError` вместо задуманного `ExternalCallError`. Ловится общим `except Exception`
в исполнителе тулов, поэтому долетает до LLM как `"KeyError: 'city'"` вместо
внятного `"missing required params: city"` — функционально работает, но
сбивающая с толку поверхность ошибки для спеки эндпоинта, заведённой админом.

**Fix:** требовать `required: true` на этапе валидации спеки для любого поля,
упомянутого в `url_template`.

## 9. Мелкое: `RouteDecision.ops == ()` задокументирован как passthrough, а реализован как неявный `START_NEW` — `agent/router.py:33`, `agent/runner.py:373`

```python
ops = decision.ops or (RouteOp(action=RouteAction.START_NEW),)
```

Докстринг говорит, что пустой пакет — passthrough (no-op); фактическое поведение —
старт нового foreground-процесса. Влияние небольшое (возможно, это и есть
задуманный fallback), но докстринг и код расходятся — стоит сверить в одну сторону.

## Проверено, багов не найдено

Owner-изоляция запросов datasets, жизненный цикл сессий БД (commit/rollback/close),
пути обхода JSON-schema валидации, обработка редиректов в SSRF-гарде, математика
косинус-ранжирования — прочитаны целиком, конкретных багов не выявлено.

## Приоритизация

№1 (обход SSRF) и №3 (крон на паузе всё равно срабатывает) — security/correctness,
закрывать в первую очередь. №2 и №5 — надёжность процессной модели, видимая
пользователю. №4 — баг UX. №6–8 — точечные, среднего/низкого риска. №9 — минимальная
сверка докстринга с кодом.
