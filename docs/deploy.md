# Деплой: Postgres и бот в контейнерах

Целевая топология — `docker compose`: сервис `postgres` держит состояние, сервис `telegram`
крутит бота (только исходящие соединения, порт не слушается). Web-поверхность лежит за
профилем и по умолчанию не поднимается:

```bash
docker compose up -d              # postgres + telegram
docker compose --profile web up -d  # плюс web на :8000
```

Образ один на оба сервиса (`octoforge:local`), различаются только `command` и порты. У
web-сервиса `OF_TELEGRAM_BOT_TOKEN` принудительно пуст: два поллера на одном токене получают от
Bot API `Conflict: terminated by other getUpdates request`, поэтому бота владеет ровно один
сервис.

## Что где лежит

| | |
|---|---|
| Базы | `octoforge` (приложение), `octoforge_telegram` (инвайты), `octoforge_dev` (локальный запуск), `octoforge_test` (тесты) |
| Том с кластером | `pg-data` → `/var/lib/postgresql` (у образа 18+ данные в подкаталоге мажора) |
| Кэш моделей | bind-mount `~/.cache/huggingface` — иначе контейнер заново тянет 1.1 GB эмбеддера |
| Секреты | `.env` (LLM-ключ, токен бота, serper); держи `chmod 600` |
| Порт Postgres | `127.0.0.1:5432` — только loopback, для `psql`/`pg_dump` с хоста |

## Переезд с SQLite (одноразовый)

Порядок важен: пока живы писатели, снятая копия догоняется новыми строками.

1. **Погасить всех писателей.** Проверить, что нет второй инстанции бота с тем же токеном:
   ```bash
   ps -eo pid,lstart,args | grep "[o]ctoforge_web.telegram"
   ```
   Признак конфликта в логе — `Conflict: terminated by other getUpdates request`.
2. **Атомарный снапшот** (не `cp`: копия живого файла не консистентна):
   ```bash
   .venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:octoforge.db?mode=ro',uri=True); d=sqlite3.connect('backup-octoforge.db'); s.backup(d)"
   ```
   То же для `telegram.db`.
3. **Поднять Postgres:** `make db-up`.
4. **Перенести данные:**
   ```bash
   .venv/bin/python tools/sqlite_to_postgres.py \
     --source sqlite+aiosqlite:///./backup-octoforge.db \
     --target postgresql+asyncpg://octoforge:octoforge@127.0.0.1:5432/octoforge \
     --invite-source sqlite+aiosqlite:///./backup-telegram.db \
     --invite-target postgresql+asyncpg://octoforge:octoforge@127.0.0.1:5432/octoforge_telegram
   ```
   Скрипт сверяет счётчики по каждой таблице и возвращает ненулевой код при расхождении.
5. **Поднять бота:** `docker compose up -d telegram`, затем `docker compose logs -f telegram` —
   ждём `Telegram surface is up without the HTTP API`.
6. **Проверить живьём:** написать боту сообщение и дождаться ближайшего срабатывания крона
   (`docker compose exec postgres psql -U octoforge -d octoforge -c 'SELECT title, next_fire_at, last_status FROM cron_jobs ORDER BY next_fire_at'`).

Откат: остановить контейнер, вернуть `OF_DATABASE_URL` на SQLite-файл, запустить нативно.
SQLite-файлы после переноса не удаляются — это и есть точка возврата.

## Эксплуатация

- **Бэкапы:** `tools/pg_backup.sh [каталог]` — дампит `octoforge` и `octoforge_telegram`
  gzip'ом и оставляет последние `KEEP` файлов (по умолчанию 14). `pg_dump` консистентен на
  живой базе, в отличие от копирования файлов. Для расписания — systemd-таймер, пример в
  шапке скрипта (`systemctl --user enable --now`, плюс `loginctl enable-linger`, чтобы жил
  после логаута).
- **Логи:** `docker compose logs -f telegram`. Ротацию делает docker (`json-file` по умолчанию).
- **Перезапуск после падения хоста:** `restart: unless-stopped` плюс включённый
  `docker.service` поднимают стек сами.
- **Обновление кода:** `docker compose build && docker compose up -d`. Схема доводится на
  старте (`bootstrap_schema`), отдельного шага миграций не нужно.
- **Не запускать** дев-процесс на базе `octoforge`: для локальных прогонов есть `octoforge_dev`,
  а для тестов `octoforge_test` (фикстура дропает его схему).
