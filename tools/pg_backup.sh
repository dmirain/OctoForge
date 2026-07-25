#!/bin/sh
# Dump the compose Postgres databases, keeping the last N files.
#
# pg_dump is consistent against a live server, unlike copying data files — the
# reason the old SQLite snapshots taken with `cp` were unreliable.
#
# Usage:  tools/pg_backup.sh [target-directory]
# Cron/systemd example (daily, keeping two weeks):
#   [Unit] Description=OctoForge database backup
#   [Service] Type=oneshot
#   WorkingDirectory=%h/dev/OctoForge
#   ExecStart=%h/dev/OctoForge/tools/pg_backup.sh %h/octoforge-backups
# plus a matching .timer with OnCalendar=daily and Persistent=true, enabled via
# `systemctl --user enable --now octoforge-backup.timer` (needs
# `loginctl enable-linger` to survive logout).
set -eu

TARGET_DIR="${1:-./backups}"
KEEP="${KEEP:-14}"
USER_NAME="${POSTGRES_USER:-octoforge}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

mkdir -p "${TARGET_DIR}"

for database in "${POSTGRES_DB:-octoforge}" "${POSTGRES_TELEGRAM_DB:-octoforge_telegram}"; do
    out="${TARGET_DIR}/${database}-${STAMP}.sql.gz"
    # -T: no TTY, so the stream stays binary-clean through the pipe
    docker compose exec -T postgres pg_dump -U "${USER_NAME}" "${database}" | gzip >"${out}"
    echo "${out} ($(du -h "${out}" | cut -f1))"

    # rotation: drop everything past the newest ${KEEP} dumps of this database
    ls -1t "${TARGET_DIR}/${database}"-*.sql.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
        echo "removing ${old}"
        rm -f "${old}"
    done
done
