# Деплой: Postgres, приложение и TLS в контейнерах

Целевая топология — `docker compose`:

```bash
docker compose up -d   # postgres + app + caddy
```

- **postgres** — состояние;
- **app** — один процесс: HTTP API, консоль оператора и Telegram-бот (бот поднимается в том же
  процессе, потому что `OF_TELEGRAM_BOT_TOKEN` задан). Порт наружу не публикуется, только
  `expose: 8000` во внутреннюю сеть;
- **caddy** — терминирует TLS на 80/443, сам получает и продлевает сертификат Let's Encrypt.

Почему обе поверхности в одном процессе: разнеси их по контейнерам — получишь два
cron-планировщика и два recovery-sweep'а на одной базе, а ни один из них не фильтрует по
`channel`. Тогда срабатывание крона телеграм-пользователя может выполнить web-процесс, у чьего
раннера нет бриджа для доставки. Для деплоя вообще без HTTP остался профиль:
`docker compose --profile standalone up -d telegram`.

## TLS и сертификат

Всё делает Caddy (`docker/Caddyfile`): HTTP-01 на :80, редирект 80→443, продление без certbot,
без cron-задач и без хуков. Сертификаты и ACME-аккаунт лежат в томе `caddy-data` — потеряешь
том, и выдача начнётся заново (у Let's Encrypt есть лимиты). Перед первым боевым запуском
имеет смысл проверить доступность порта на staging-эндпоинте:

```bash
ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory docker compose up -d caddy
docker compose logs caddy | grep "certificate obtained"
docker compose up -d --force-recreate caddy   # снова боевой CA
```

## Консоль оператора и аутентификация

`https://<SITE_DOMAIN>/admin.html` — таблицы по всем сущностям (диалоги и их сообщения, задачи,
cron, инструкции, датасеты и записи, память, сводки) плюс действия: пауза/возобновление и
удаление cron-задач, удаление задач и памяти, публикация инструкции.

Аутентификация — HTTP Basic поверх TLS, одна операторская пара логин/пароль
(`OF_ADMIN_USERNAME`, `OF_ADMIN_PASSWORD_HASH`). Гейт стоит middleware'ом на **всём**, кроме
`/health` и `/health/ready`: чат-UI, `/api/dialog/*`, `/docs`, статика. Это не система
пользователей — `X-User-Id` по-прежнему выбирает диалог; но на публично доступном хосте без
пароля любой мог бы подставить чужой id и читать чужие диалоги.

Сменить пароль:

```bash
python tools/hash_password.py            # печатает пароль и строку для .env
# заменить OF_ADMIN_PASSWORD_HASH в .env
docker compose up -d --build app         # именно --build: хэш читается кодом из образа
```

## Что где лежит

| | |
|---|---|
| Базы | `octoforge` (приложение), `octoforge_telegram` (инвайты), `octoforge_dev` (локальный запуск), `octoforge_test` (тесты) |
| Сертификаты | том `caddy-data` (плюс `caddy-config`) |
| Том с кластером | `pg-data` → `/var/lib/postgresql` (у образа 18+ данные в подкаталоге мажора) |
| Кэш моделей | bind-mount `~/.cache/huggingface` — иначе контейнер заново тянет 1.1 GB эмбеддера |
| Секреты | `.env` (LLM-ключ, токен бота, serper, хэш пароля консоли); держи `chmod 600`. Значения не должны содержать `$` — compose его интерполирует |
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
5. **Поднять стек:** `docker compose up -d`, затем `docker compose logs -f app` — ждём
   `Application startup complete`.
6. **Проверить живьём:** написать боту сообщение и дождаться ближайшего срабатывания крона
   (`docker compose exec postgres psql -U octoforge -d octoforge -c 'SELECT title, next_fire_at, last_status FROM cron_jobs ORDER BY next_fire_at'`).

Откат: остановить контейнер, вернуть `OF_DATABASE_URL` на SQLite-файл, запустить нативно.
SQLite-файлы после переноса не удаляются — это и есть точка возврата.

## Эксплуатация

- **Бэкапы:** `tools/pg_backup.sh [каталог]` — дампит `octoforge` и `octoforge_telegram`
  gzip'ом и оставляет последние `KEEP` файлов (по умолчанию 14). `pg_dump` консистентен на
  живой базе, в отличие от копирования файлов. По расписанию это делает systemd-таймер
  пользователя (`~/.config/systemd/user/octoforge-backup.{service,timer}`, ежедневно,
  `Persistent=true` догоняет пропущенный запуск, дампы в `~/octoforge-backups`):

  ```bash
  systemctl --user list-timers octoforge-backup.timer
  systemctl --user start octoforge-backup.service   # разовый прогон
  journalctl --user -u octoforge-backup.service -n 20
  ```

  Два подвоха. Первый: `ExecStart` идёт через `sg docker -c`, потому что user-менеджер
  systemd стартовал до того, как аккаунт попал в группу `docker`, и юниты наследуют старый
  набор групп — без `sg` служба не достаёт `/var/run/docker.sock`. Второй: нужен
  `sudo loginctl enable-linger $USER`, иначе user-менеджер (а с ним таймер) не работает без
  активной сессии и не поднимается после ребута.

- **Проверка бэкапа:** восстановление в отдельную базу, а не «файл есть, значит порядок»:
  ```bash
  docker compose exec -T postgres psql -U octoforge -d postgres -c 'CREATE DATABASE restore_check OWNER octoforge'
  gunzip -c ~/octoforge-backups/octoforge-ГГГГММДД-ЧЧММСС.sql.gz \
    | docker compose exec -T postgres psql -q -U octoforge -d restore_check
  ```
- **Логи:** `docker compose logs -f app`. Ротацию делает docker (`json-file` по умолчанию).
  Логгер `httpx` принудительно на WARNING: на INFO он печатает полный URL запроса, а у Bot API
  в URL лежит токен.
- **Перезапуск после падения хоста:** `restart: unless-stopped` плюс включённый
  `docker.service` поднимают стек сами.
- **Обновление кода:** `docker compose build && docker compose up -d`. Схема доводится на
  старте (`bootstrap_schema`), отдельного шага миграций не нужно.
- **Не запускать** дев-процесс на базе `octoforge`: для локальных прогонов есть `octoforge_dev`,
  а для тестов `octoforge_test` (фикстура дропает его схему).
