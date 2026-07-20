# Суммаризация контекста в openclaw (исследование кода)

> Исследование соседнего репозитория openclaw (TypeScript-монорепо, pnpm),
> снято 2026-07-20. Механизм называется **compaction**. Все ссылки — на файлы
> openclaw. Применимость к нам — в последнем разделе; наш дизайн компакции —
> [context.md](context.md), обзор openclaw целиком — [openclaw-review.md](openclaw-review.md).

## Где живёт

Три слоя:

- **Ядро алгоритма** (общая библиотека):
  `packages/agent-core/src/harness/compaction/compaction.ts` — выбор точки среза,
  подсчёт токенов, LLM-вызовы: `shouldCompact` / `prepareCompaction` / `compact` /
  `generateSummary`. Поверх — мост совместимости `src/agents/sessions/compaction/compaction.ts`.
- **Стадийный пайплайн и фолбэки**: `src/agents/compaction.ts` (`summarizeInStages`,
  :379) и планирование/математика токенов `src/agents/compaction-planning.ts`.
- **Режим «safeguard»** (дефолт для новых конфигов): хук `session_before_compact`
  `src/agents/agent-hooks/compaction-safeguard.ts:1049` — перехватывает компакцию,
  добавляет структурные секции, сохранение последних ходов, аудит качества.

Вокруг — плагабельная абстракция **context engine** (`src/context-engine/`):
интерфейс `ContextEngine` с хуками жизненного цикла `ingest/assemble/afterTurn/
compact/dispose` (`types.ts:128-152`); встроенный `LegacyContextEngine`
(`legacy.ts:23`) делегирует рантайму; сторонний движок может заявить
`ownsCompaction` (`types.ts:198`). Плагины могут подменять только шаг
суммаризации — провайдер компакции (`src/plugins/compaction-provider.ts`).

## Триггер: когда срабатывает

Проверяется **на каждый ход** (не по таймеру), тремя путями:

1. **Порог после хода и перед следующим промптом.** `checkCompaction()`
   (`src/agents/sessions/agent-session-compaction.ts:232`) вызывается после
   успешного хода и перед отправкой промпта. Предикат
   (`packages/agent-core/src/harness/compaction/compaction.ts:243-252`):

   ```
   contextTokens > contextWindow - reserveTokens
   ```

   `contextTokens` — реальный usage провайдера из последнего ответа ассистента
   (или оценка, если usage недоступен).
2. **Восстановление после overflow-ошибки модели.** Модель вернула
   context-overflow → одна компакция + авторетрай хода; повторный overflow —
   явная ошибка (`agent-session-compaction.ts:269-290`).
3. **Превентивная проверка перед промптом.** Оценка давления промпта выбирает
   маршрут `fits | compact_only | truncate_tool_results_only |
   compact_then_truncate` (`embedded-agent-runner/run/preemptive-compaction.ts:311-381`).
   Опционально — такая же проверка посреди tool-цикла (`compaction.midTurnPrecheck`,
   по умолчанию выключена).

Ручной триггер: команда `/compact [инструкции]`.

### Конфиг (секция `agents.defaults.compaction`, Zod: `src/config/zod-schema.agent-defaults.ts:159-206`)

| Knob | Default | Смысл |
|---|---|---|
| `enabled` | true | выключатель |
| `reserveTokens` | 16384 | резерв окна; порог = окно − резерв |
| `keepRecentTokens` | 20000 | сколько хвоста оставить дословно |
| `reserveTokensFloor` | 20000 | нижняя граница эффективного резерва |
| `mode` | `"safeguard"` (новые конфиги) | `"default"` \| `"safeguard"` |
| `model` | — | отдельная модель для суммаризации |
| `timeoutSeconds` | 180 | таймаут вызова суммаризации |
| `recentTurnsPreserve` | 3 (max 12) | последние N ходов вербатим в суффиксе |
| `maxHistoryShare` | 0.5 | макс. доля окна под несжатый хвост |
| `customInstructions`, `identifierPolicy`, `qualityGuard`, `memoryFlush`, `truncateAfterCompaction`, `notifyUser`, `postCompactionSections` | — | см. раздел «Дополнительные механики» |

Эффективный резерв дополнительно клампится так, чтобы под промпт оставалось
минимум `min(8000, 50% окна)` (`src/agents/agent-settings.ts:70-88`) — защита
маленьких контекстных окон.

## Механизм: как производится саммари

Отдельный LLM-вызов (или несколько) — моделью сессии либо `compaction.model`,
стримом через обычный стек (`compaction.ts:564-575`).

**Промпт** (`generateSummary`, `compaction.ts:635-676`):

- Системный: «You are a context summarization assistant. Your task is to read a
  conversation between a user and an AI assistant, then produce a structured
  summary following the exact format specified. Do NOT continue the conversation…»
- Транскрипт сериализуется и заворачивается в `<conversation>…</conversation>`;
  при инкрементальном режиме добавляется `<previous-summary>…</previous-summary>`.
- Свежая суммаризация (`SUMMARIZATION_PROMPT`, :474-505) требует точный формат
  секций: `## Goal`, `## Constraints & Preferences`, `## Progress`
  (Done / In Progress / Blocked), `## Key Decisions`, `## Next Steps`,
  `## Critical Context`; финальная строка: «Preserve exact file paths, function
  names, and error messages.»
- **Инкрементальный режим** (`UPDATE_SUMMARIZATION_PROMPT`, :507-544): «The
  messages above are NEW conversation messages to incorporate into the existing
  summary provided in <previous-summary> tags… PRESERVE all existing
  information… UPDATE the Progress section». То есть от компакции к компакции —
  саммари-оф-саммари. (В safeguard-режиме вместо этого прежнее саммари
  подклеивается сообщением для **переварки заново**:
  `PREVIOUS_SUMMARY_REDISTILL_PREFIX`, `compaction-safeguard.ts:79-108`.)
- Потолок вывода: `maxTokens = min(0.8 × reserveTokens, model.maxTokens)`.

**Чанкинг больших историй** (`summarizeInStages`, `src/agents/compaction.ts:379-492`):
история режется на части по токенам (по умолчанию 2; размер — адаптивная доля
окна `BASE_CHUNK_RATIO = 0.4`, минимум `0.15`, минус
`SUMMARIZATION_OVERHEAD_TOKENS = 4096`). Каждый чанк суммаризуется **итеративно,
с протаскиванием накопленного саммари** (`summarizeChunks`, :159-226), затем
финальный merge-вызов (`MERGE_SUMMARIES_INSTRUCTIONS`, :51-64: «Merge these
partial summaries into a single cohesive summary… MUST PRESERVE: Active tasks…
Batch operation progress… PRIORITIZE recent context over older history»).
Чанки помечаются `[Chunk 1 — oldest messages <UTC range>]`.

**Split turn**: если срез попал внутрь текущего хода, суффикс остаётся вербатим,
а префикс хода суммаризуется отдельным `TURN_PREFIX_SUMMARIZATION_PROMPT`
(:779-792) и приклеивается как `**Turn Context (split turn):**`.

**Safeguard-контракт саммари** — фиксированные заголовки `## Decisions`,
`## Open TODOs`, `## Constraints/Rules`, `## Pending user asks`,
`## Exact identifiers` (`REQUIRED_SUMMARY_SECTIONS`,
`compaction-safeguard-quality.ts:14-20`) + требование «Preserve all opaque
identifiers exactly as written… UUIDs, hashes, IDs, hostnames, IPs, ports, URLs,
and file names».

## Склейка результата в контекст

Сессия — **дерево записей** (сообщения, compaction-записи, метки). Компакция:

1. `findCutPoint()` (`compaction.ts:403-468`): идём от новых записей назад,
   накапливая токены до `keepRecentTokens`; срез — **только на валидных
   границах** (user/assistant/custom/bashExecution, никогда на `toolResult`,
   `findValidCutPoints` :322-366) — пары tool_call/tool_result не разрываются.
2. Всё до среза суммаризуется; записи от `firstKeptEntryId` — остаются вербатим.
3. Результат добавляется в сессию как **запись типа `compaction`**
   (`appendCompaction(summary, firstKeptEntryId, tokensBefore, details, fromHook)`,
   `packages/agent-core/src/harness/session/session.ts:190-208`). **Старые
   сообщения не удаляются** — саммари лишь новый узел дерева.
4. При сборке контекста `buildSessionContext()` (`session.ts:28-102`) выдаёт:
   последнее саммари как синтетическое сообщение с ролью `compactionSummary`,
   затем записи от `firstKeptEntryId` и всё после compaction-записи. На границе
   LLM `convertToLlm` превращает саммари в **user-сообщение**
   (`packages/agent-core/src/harness/messages.ts:42-48,165-175`):

   ```
   The conversation history before this point was compacted into the following summary:

   <summary>
   …
   </summary>
   ```

5. **Суффиксы safeguard-режима** (`assembleSuffix`, `compaction-safeguard.ts:427-446`):
   split-turn контекст; `## Recent turns preserved verbatim` (последние
   `recentTurnsPreserve`=3 хода user/assistant усечёнными строками);
   дайджест упавших тулов; списки прочитанных/изменённых файлов (извлекаются из
   истории); опциональные секции AGENTS.md (`postCompactionSections`). Тело
   саммари капается на `MAX_COMPACTION_SUMMARY_CHARS = 16_000` с сохранением
   суффикса.

## Хранение

Саммари персистится **прямо в транскрипте сессии** как compaction-запись
(`session.ts:197-207`), отдельного стора нет. Транскрипты — JSONL-файлы
(`…_<sessionId>.jsonl`), в новых рантаймах — SQLite. В записи: `summary`,
`firstKeptEntryId`, `tokensBefore`, `details` (списки файлов), `fromHook` —
достаточно и для пересборки контекста, и для инкрементального обновления
(`previousSummary` читается обратно в `prepareCompaction`, :715-724). Полная
докомпакционная история остаётся на диске — компакция меняет только то, что
видит модель. Опция `truncateAfterCompaction` ротирует транскрипт: новый
файл-наследник, посеянный саммари + хвостом. В индексе сессий счётчики
`compactionCount` / `memoryFlushCompactionCount` — для разовых хозработ на цикл.

## Подсчёт токенов

Два оценщика:

- **Авторитетный** (триггер порога): реальный usage провайдера из последнего
  ответа — `usage.contextUsage.totalTokens`, иначе `input+output+cacheRead+
  cacheWrite` (`compaction.ts:150-155`); сообщения после последнего usage
  дооцениваются (`estimateContextTokens`, :212-240).
- **Эвристический** (срезы, чанкинг, пречек): `estimateTokens = ceil(chars/4)`,
  поблочно (text, thinking, toolCall name+JSON args; картинка = 4800 символов),
  :254-321. Пречек дифференцирует ставки: текст 4 символа/токен, tool results 2,
  JSON 3, +12 токенов на сообщение, +6 на блок, картинка 2000
  (`preemptive-compaction.ts:26-31`), всё × `SAFETY_MARGIN = 1.2`
  (`compaction-planning.ts:17`). Перед оценкой сообщения **санитизируются** —
  toolResult `details` и runtime-контекст вырезаются из соображений
  безопасности (`compaction-planning.ts:51-74`).

## Дополнительные механики

- **Сохранение tool-пар на двух уровнях**: при нарезке чанков `tool_use` и его
  `toolResult` держатся в одном чанке сдвигом границ
  (`splitMessagesByTokenShare`, `compaction-planning.ts:117-197`); после
  выброса старых чанков осиротевшие результаты чинятся/выбрасываются
  (`repairToolUseResultPairing`, :384-403).
- **Прунинг истории**: если несжатый хвост после компакции превышает
  `maxHistoryShare` (0.5) окна, старейшие чанки исключаются из суммаризации и
  уходят в отдельное `droppedSummary` (`buildHistoryPrunePlan`, :417-453).
- **Оверсайз-сообщения**: одно сообщение >50% окна исключается и заменяется
  заглушкой `[Large <role> (~NK tokens) omitted from summary]`
  (`isOversizedForSummary`, :274-277).
- **Каскад фолбэков** (`src/agents/compaction.ts:268-345`): ретраи чанков с
  backoff (3 попытки) → частичное саммари с пометкой, какие чанки упали →
  ретрай с исключением оверсайза → generic-заглушка «Context contained N
  messages…»; circuit breaker обрывает после 2 generic-фолбэков подряд.
  Safeguard при фейле переварки оставляет прежнее саммари вербатим
  (`compaction-safeguard.ts:417-420`) или пишет структурную заглушку, чтобы
  разорвать цикл ретриггеров (:1087-1097).
- **Quality guard** (safeguard): `auditSummaryQuality` проверяет обязательные
  секции, сохранность идентификаторов и последний запрос пользователя; при
  провале — пересуммаризация с фидбэком, до `qualityGuard.maxRetries`
  (по умолчанию 1, max 3) (`compaction-safeguard.ts:1421-1450`).
- **Memory flush перед компакцией**: на мягком пороге `окно − reserveTokensFloor
  − softThresholdTokens` (soft = 4000, `extensions/memory-core/src/flush-plan.ts:12`)
  молча (без вывода пользователю) прогоняется агентный ход с промптом
  «Pre-compaction memory flush. Store durable memories only in
  memory/YYYY-MM-DD.md…» — долговременные знания спасаются до сжатия; раз на
  компакционный цикл, фейл терпим (3 попытки) (`src/auto-reply/reply/memory-flush.ts:121-161`).
- **Per-model адаптация**: эффективный резерв клампится для малых окон
  (см. выше); суммаризация может идти другой моделью и наследует fallback-цепочку
  моделей сессии при ошибках провайдера; учитываются серверные пороги компакции
  OpenAI Responses.
- **Хуки наблюдаемости**: `session_before_compact` (может отменить или подставить
  своё саммари — точка расширения safeguard'а), `session_compact`,
  `compaction_start` / `compaction_end`.

## Смежные, но отдельные механизмы

- **Прунинг tool-результатов** (без суммаризации, только в памяти):
  `tool-result-truncation.ts`, выбирается маршрутом `truncate_tool_results_only`
  пречека.
- **Branch summarization**: саммари при навигации по веткам дерева сессии
  (`packages/agent-core/src/harness/compaction/branch-summarization.ts`),
  рендер с `BRANCH_SUMMARY_PREFIX` (`messages.ts:50-55`).

## Сопоставление с нашим дизайном (context.md)

Что совпадает концептуально: полный архив не удаляется (у них — транскрипт на
диске, у нас — таблица `messages`, уровень 1); сжатое прошлое + дословный свежий
хвост; суммаризация отдельным LLM-вызовом со структурным промптом; компакция как
фоновая работа, не блокирующая диалог.

Чем openclaw богаче (и где у нас это осознанно отложено или не нужно):

- **Триггер по честным токенам** (usage провайдера + эвристика chars/4 с
  запасом ×1.2). У нас: лимит в символах (`OF_CONTEXT_HOT_MAX_CHARS`),
  честные токены — отдельная итерация (context.md «Не входит»). У них к тому же
  три пути триггера: порог, overflow-ретрай, превентивный пречек.
- **Инкрементальное саммари-оф-саммари** через `<previous-summary>`. У нас —
  независимые саммари сегментов с тегами тем (все в промпт целиком); мердж —
  понадобится при росте числа тем.
- **Срез только на безопасных границах** (не разрывать tool_call/tool_result) —
  **учтено** в context.md: срез нарратива — целыми сообщениями (tool-пар в нём нет
  по построению), salvaged-пара «прерванный ответ + заметка о неполноте» не
  разделяется, а для будущей компакции веток прогонов правило неразрыва
  tool-пар зафиксировано как обязательное.
- **Суффиксы**: последние N ходов верbatim + списки файлов/фейлов. У нас хвост
  и так дословный; аналогичный суффикс имеет смысл только если решим резать и
  его.
- **Memory flush на мягком пороге** — спасение знаний до сжатия. Для нас
  естественное отображение — прогон «сохрани важное в memory» перед компакцией;
  отдельная идея, в context.md не входит.
- **Quality guard и каскад фолбэков** — у нас фейл суммаризации = warning и
  мягкая деградация (хвост растёт до следующего триггера). Для старта достаточно.
- **Плагабельность**: у них context engine + провайдеры компакции; у нас —
  порт `ContextCompactor` (DI), чего достаточно по нашей модульности.

Минимальный рецепт, который стоит взять независимо от объёма: триггер
«окно − резерв» с оценкой по usage провайдера и фолбэком chars/4; срез на
безопасной границе; структурный промпт (Goal/Progress/Decisions/Next Steps +
«preserve exact identifiers»); инкрементальное обновление прежнего саммари;
саммари как первое сообщение ветки + дословный хвост.
