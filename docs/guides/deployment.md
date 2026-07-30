# Deployment

Running OctoForge for real: one host, three containers, TLS obtained automatically, Postgres behind them.

## Topology

`docker compose up -d` starts:

| Service | What it does |
|---|---|
| `postgres` | `postgres:18-alpine`. Published on `127.0.0.1:5432` only, so `psql` and `pg_dump` work from the host without exposing the server. Its init script creates `octoforge_telegram`, `octoforge_test` and `octoforge_dev` next to `octoforge` |
| `app` | One process serving the HTTP API, the operator console **and** the Telegram bot. No published port — Caddy reaches it over the compose network, so it never answers without TLS |
| `caddy` | Terminates TLS for `SITE_DOMAIN`, obtaining and renewing a Let's Encrypt certificate itself. Adds HSTS and the usual hardening headers, and forwards credentials untouched |

`--profile standalone` runs a bot-only service instead, for a deployment with no HTTP surface at all.

**Why one process for both surfaces:** splitting web and Telegram across two containers would put two cron
schedulers and two recovery sweeps on one database, and neither filters by channel — a Telegram user's cron
firing could be executed by the web process, whose runner has no bridge to deliver it.

## Before the first start

```bash
cp .env.example .env
python tools/hash_password.py           # OF_ADMIN_USERNAME / OF_ADMIN_PASSWORD_HASH
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # OF_SECRETS_KEY
```

Set at least:

```
OF_LLM_BASE_URL=…       OF_LLM_API_KEY=…       OF_LLM_MODEL=…
OF_ADMIN_USERNAME=…     OF_ADMIN_PASSWORD_HASH=…
OF_SECRETS_KEY=…
SITE_DOMAIN=example.org ACME_EMAIL=you@example.org
OF_SELF_BASE_URL=https://example.org
```

Two traps worth knowing before they cost you an hour:

- **docker compose interpolates `$` in `.env`.** A value containing one arrives mangled in the container.
  Keep secrets `$`-free (the password hash uses `:` separators for exactly this reason) or escape as `$$`.
- **The compose file overrides the two database URLs** to reach Postgres over the internal network. A
  host-side SQLite path left in `.env` would otherwise create a throwaway file inside the container.

While checking that inbound port 80 works, point `ACME_CA` at Let's Encrypt's staging endpoint — its rate
limits are loose. Unset it for the real certificate.

## Verifying a deployment

1. `docker compose logs app | head -40` — read the capability report. Everything you expect to be on should
   say `on`, with the endpoint it uses.
2. `curl -sf https://<domain>/health` — liveness.
3. `curl -sf https://<domain>/health/ready` — readiness, which touches the database.
4. Open `/admin.html` and log in with the operator credential.
5. Send one real message and watch it answer.

Logs go to stdout/stderr only (no file handler); collect them with the container runtime.

## Day-2 operations

**Backups.** `tools/pg_backup.sh [dir]` dumps both databases with `pg_dump` (consistent against a live
server, unlike copying data files) and keeps the last `KEEP=14`. The script's header carries a
systemd timer example. Test a restore before you need one.

**Upgrades.** Pull, rebuild, restart: `docker compose up -d --build`. Alembic runs at startup and brings the
schema to head; migrations are append-only, so a rollback of code across a migration boundary needs a
deliberate down-migration or a restore.

**Certificates** live in the `caddy-data` volume together with the ACME account. Losing that volume means
re-issuing — and burning rate limit. Keep it.

**Moving from SQLite to Postgres.** `tools/sqlite_to_postgres.py` copies table by table in foreign-key order
and verifies row counts, handling the timezone difference through the ORM rather than moving raw values. Stop
every writer first: a bot still appending rows will fail the count check.

**Scaling.** The cron scheduler claims firings with a SQL compare-and-swap lease, so several instances can
run it safely. Everything else assumes one writer per dialog; Postgres is required for more than one process
(SQLite allows exactly one writer). One asyncio loop serves every dialog of a process — see
[performance.md](performance.md) for what that implies.

## Hardening checklist

- `OF_ADMIN_PASSWORD_HASH` set — an empty one means every request answers 503, which is the safe direction
  but not a working deployment.
- `OF_TELEGRAM_ADMIN_IDS` set **before** handing the bot's name to anyone: while it is empty the invite gate
  is inactive and the bot answers everyone.
- The HTTP surface authenticates the *operator*, not your employees. If people other than operators will use
  the web UI, put an authenticating proxy in front and have it set `X-User-Id` — see
  [../security.md](../security.md).
- Postgres stays on loopback. Do not publish 5432 to the network.
- `OF_SECRETS_KEY` backed up somewhere separate from the database dump: losing it makes every stored secret
  unreadable, having both in one place defeats the encryption.

## Code anchors

- `docker-compose.yml`, `Dockerfile`, `docker/Caddyfile` — the topology
- `docker/postgres-init/` — the extra databases
- `tools/pg_backup.sh`, `tools/sqlite_to_postgres.py` — backups and the one-off migration
- `web/src/octoforge_web/main.py` — startup, migrations, health probes
