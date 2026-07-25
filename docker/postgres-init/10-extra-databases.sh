#!/bin/sh
# Create the databases the entrypoint does not: it only ever creates
# POSTGRES_DB. Runs once, when the pg-data volume is initialized.
#
# - the Telegram invite store keeps its own database (its own Base, no Alembic,
#   never touching core's schema — see OF_TELEGRAM_DATABASE_URL)
# - the test database is separate because core/tests/test_postgres_stores.py
#   drops and recreates the public schema before every test
# - the dev database is where a locally run app points, so development never
#   opens the deployment database
set -eu

for database in \
    "${POSTGRES_TELEGRAM_DB:-octoforge_telegram}" \
    "${POSTGRES_TEST_DB:-octoforge_test}" \
    "${POSTGRES_DEV_DB:-octoforge_dev}"; do
    echo "creating database ${database}"
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<SQL
CREATE DATABASE "${database}" OWNER "${POSTGRES_USER}";
SQL
done
