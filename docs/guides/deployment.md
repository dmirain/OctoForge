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


## Upgrading a deployment that predates the search extensions

The Postgres service builds from `docker/postgres/`, a thin layer on `pgvector/pgvector:pg18` that adds
`pg_textsearch`. Two things are worth knowing before switching an existing deployment onto it.

**The base image changed libc.** Older deployments ran `postgres:18-alpine` (musl); this one is Debian
(glibc). The database's default collation is libc-based, and musl and glibc order text differently, so
every B-tree index on a text column is left sorted by the wrong rules — lookups start missing rows that
are really there. Postgres does **not** warn about it here: musl records no collation version, so there
is nothing for it to compare against.

Reindexing repairs the indexes, but a cluster initialised by musl cannot have its collation version
recorded afterwards (`ALTER DATABASE ... REFRESH COLLATION VERSION` refuses), so the next libc change
would be just as silent. Restoring into a fresh cluster avoids both problems:

```sh
tools/pg_backup.sh ~/octoforge-backups     # while the app is stopped, for a consistent dump
docker compose stop app
docker compose stop postgres && docker compose rm -f postgres
docker volume rm <project>_pg-data         # keep a copy first if you want a way back
docker compose up -d --wait postgres       # fresh initdb under glibc, init scripts recreate the databases
zcat ~/octoforge-backups/<db>-<stamp>.sql.gz | docker compose exec -T postgres psql -U octoforge -d <db>
docker compose start app                   # migrations create the extensions and indexes
```

Compare row counts before and after, and check the startup report says `vector search on` and
`lexical search on`.

**A deployment that cannot install the extensions still works.** Managed Postgres cannot set
`shared_preload_libraries`, so `pg_textsearch` is unavailable there; an application role that is not a
superuser cannot create any of them. Both are supported: recall falls back to in-process ranking and
`history_search` to a substring match. Nothing fails at startup.

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

**Scaling.** Postgres is required for more than one process (SQLite allows exactly one writer); which
process runs a dialog is settled by a claim, and the cron scheduler leases its firings with a SQL
compare-and-swap. One asyncio loop serves every dialog of a process — see
[performance.md](performance.md) for what that implies, and "More than one pod" below for the
arrangement.

## More than one pod

A second machine running the app, with the first one balancing across both. What this buys is
throughput and an upgrade that does not take the service down; it is **not** high availability —
the database primary and the domain both still live on the first host, so losing it is a manual
failover either way (see below).

Three roles, and it is worth being precise about which process is which:

| Role | Runs | Where |
|---|---|---|
| **Balancer** | Caddy, TLS, affinity | the host the domain points at |
| **Pod** | the app: dialogs, the agent loop, the HTTP API, and every model call — including describing an incoming picture and transcribing a recording | that host and every other one (`docker-compose.pod.yml`) |
| **Ingestion** | the Telegram long poll, the invite gate; it asks the pods to understand media rather than calling a model itself | exactly one host (`--profile ingest`) |

**The pods reach one database.** `OF_PRIMARY_HOST` points every pod at the **primary** over the
private network — never at a standby. Replication is asynchronous, so a pod reading one would see a
stale `dialog_claims` table and two processes would each conclude they own a dialog, which is the
exact corruption [../reference/dialog-ownership.md](../reference/dialog-ownership.md) exists to
prevent.

Check what `pg_hba.conf` already says before adding to it. The stock Postgres image ends it with
`host all all all scram-sha-256`, so a pod needs no new rule — and, more to the point, **that line
is what your access control actually is**: any address reaching the port may authenticate to any
database. What limits the blast radius is `POSTGRES_PEER_BIND`, which decides who can reach the port
at all. If you want the tunnel to carry replication and nothing else, that permissive line has to go
first, replaced with explicit rules:

```
host    replication    replicator    10.8.0.2/32    scram-sha-256
host    all            octoforge     10.8.0.2/32    scram-sha-256   # only if a pod runs there
```

**Affinity, not distribution.** `OF_POD_UPSTREAMS` lists the pods; Caddy hashes `X-User-Id` to pick
one, so a user keeps arriving at the pod already running their dialog. Routing them elsewhere is
*safe* — a claim is takeable, and the new owner recovers the dialog — but it costs a reload of the
context, so this is a latency decision rather than a correctness one. Requests with no such header
(static files, the operator console, the secrets form, health probes) carry no per-pod state and are
spread round-robin.

The mapping is computed over the configured pod list, not the reachable one, which is what makes a
failure cheap: when a pod fails its `/health/ready` probe its users move to the survivor and
**everyone else stays put**, and when it returns they move back. Nothing reshuffles.

**What must not be duplicated.** One Telegram token may be long-polled by one process, so ingestion
runs once, outside the pods, and every pod sets `OF_TELEGRAM_POLL_IN_PROCESS=false`; the pod compose
file pins that regardless of `.env`. Everything else is already safe to run twice: cron firings are
claimed with a SQL compare-and-swap lease, and background sweeps hand back a dialog another instance
owns instead of taking it.

**Every pod must embed with the same model.** They share one vector table, the column carries no
declared dimension, and no row records which model wrote it — so a pod configured with a different
`OF_EMBEDDING_MODEL` writes vectors that mean nothing beside the others, nothing fails loudly, and
the damage stands until everything is re-embedded. If a local backend will not fit twice (it loads
~2 GB into each process), move **all** pods onto one shared remote endpoint. Never give one pod a
model of its own.

## Replication and failover

A streaming standby on a second machine, for the case where the first one is gone. It is **not** a
second writable node and must never be used as one: replication is asynchronous, so a pod reading a
standby would see a stale `dialog_claims` table and two instances would decide they both own a
dialog — the exact corruption [../reference/dialog-ownership.md](../reference/dialog-ownership.md)
exists to prevent. Every pod points at the primary. The standby is promoted by hand when the
primary is lost, and then it *is* the primary.

**The network first.** The standby reaches the primary over a private address — a WireGuard peer or
a provider's internal network — never the public internet. `POSTGRES_PEER_BIND` publishes Postgres
on that address (default loopback, i.e. off), and that publication is the real boundary: the stock
image's `pg_hba.conf` ends with `host all all all scram-sha-256`, so everything that can reach the
port may authenticate to any database. To make the tunnel carry replication only, replace that line
with a rule naming the replication role and the standby's address — otherwise a compromise of the
standby's host reads the database as easily as it replicates it.

Rotate `POSTGRES_PASSWORD` before binding anywhere but loopback. The compose default is a published
constant.

**Setting it up.** On the primary, a role and a slot:

```sql
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '...';
SELECT pg_create_physical_replication_slot('standby_one');
```

and in `pg_hba.conf` (then `SELECT pg_reload_conf()`, no restart):

```
host    replication    replicator    10.8.0.2/32    scram-sha-256
```

`wal_level=replica`, `hot_standby=on` and the sender/slot limits are already the stock defaults —
check `pg_settings` before planning a restart you may not need. On the standby, take the base backup
with the **same image** (the extensions must match) and start it:

```
pg_basebackup -h <primary> -U replicator -D $PGDATA -S standby_one -R -P -Xs -c fast
```

`-R` writes `standby.signal` and `primary_conninfo`; `-S` binds it to the slot.

**Watching it.** On the primary, `pg_stat_replication` should show the standby `streaming` with a
lag in milliseconds; `pg_replication_slots.active` should be true.

**Promotion** is `SELECT pg_promote(wait => true)`, after which the machine is a normal read-write
primary on a new timeline. Point the pods at it. Rehearse this before you need it — an unrehearsed
failover is a guess.

> **An inactive slot never releases WAL.** A standby that is down, or promoted and not rebuilt,
> makes the primary keep every segment since it left, until the disk fills. Drop the slot
> (`pg_drop_replication_slot`) as soon as a standby is gone for good, and watch
> `pg_replication_slots` — this is the one way a standby can take the primary down with it.

A standby is not a backup: a deletion replicates in milliseconds. Keep `tools/pg_backup.sh` running.

## Hardening checklist

- `OF_ADMIN_PASSWORD_HASH` set — an empty one means every request answers 503, which is the safe direction
  but not a working deployment.
- `OF_TELEGRAM_ADMIN_IDS` set **before** handing the bot's name to anyone: while it is empty the invite gate
  is inactive and the bot answers everyone.
- The HTTP surface authenticates the *operator*, not your employees. If people other than operators will use
  the web UI, put an authenticating proxy in front and have it set `X-User-Id` — see
  [../security.md](../security.md).
- Postgres stays on loopback. The one exception is `POSTGRES_PEER_BIND` for a standby or a second
  pod, and it takes a **private** address only — never a public interface, and never with the
  default password still in place.
- `OF_SECRETS_KEY` backed up somewhere separate from the database dump: losing it makes every stored secret
  unreadable, having both in one place defeats the encryption.

## Code anchors

- `docker-compose.yml`, `Dockerfile`, `docker/Caddyfile` — the topology and the balancer
- `docker-compose.pod.yml` — a node that runs the app alone
- `surfaces/telegram/src/octoforge_telegram/ingest/__main__.py` — the ingestion node
- `docker/postgres-init/` — the extra databases
- `tools/pg_backup.sh`, `tools/sqlite_to_postgres.py` — backups and the one-off migration
- `deploy/src/octoforge_deploy/main.py` — startup, migrations, health probes
