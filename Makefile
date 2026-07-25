VENV := .venv
BIN := $(VENV)/bin

.PHONY: install upgrade lint format typecheck test check test-pg db-up db-down db-psql run run-telegram

# Test database of the compose postgres service. Separate from the app database
# on purpose: the Postgres store tests drop and recreate the public schema.
PG_TEST_URL ?= postgresql+asyncpg://octoforge:octoforge@127.0.0.1:5432/octoforge_test

install:
	python3 -m venv $(VENV)
	$(BIN)/pip install -e "core[dev,local-embeddings]" -e "web[dev]"

# Bring an existing .venv up to what CI installs. `install` leaves any
# already-satisfied dependency alone, so a linter picked up weeks ago keeps
# disagreeing with CI (which resolves everything fresh on every run) until
# something bumps it.
#
# Two steps on purpose. `core[dev]` is upgraded eagerly — that is where the
# check tooling lives — while `web[dev]` only gets the default
# only-if-needed pass: it depends on octoforge-core[local-embeddings], so an
# eager resolve there would re-resolve torch for a multi-gigabyte download
# (and off the CPU-only index the Dockerfile uses). Version caps from the
# pyprojects are honored either way, unlike a bare `pip install -U ruff`.
upgrade:
	$(BIN)/pip install --upgrade --upgrade-strategy eager -e "core[dev]"
	$(BIN)/pip install --upgrade -e "web[dev]"

lint:
	$(BIN)/ruff check core/src core/tests web/src web/tests
	$(BIN)/ruff format --check core/src core/tests web/src web/tests

format:
	$(BIN)/ruff check --fix core/src core/tests web/src web/tests
	$(BIN)/ruff format core/src core/tests web/src web/tests

typecheck:
	cd core && ../$(BIN)/mypy
	cd web && ../$(BIN)/mypy

test:
	cd core && ../$(BIN)/pytest
	cd web && ../$(BIN)/pytest

check: lint typecheck test

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose stop postgres

db-psql:
	docker compose exec postgres psql -U octoforge -d octoforge

# The dialect-sensitive store tests against a real server; `make check` skips
# them (no OF_TEST_DATABASE_URL), so run this after touching db/, any *_store.py
# or a migration.
test-pg: db-up
	cd core && OF_TEST_DATABASE_URL="$(PG_TEST_URL)" ../$(BIN)/pytest tests/test_postgres_stores.py

run:
	$(BIN)/uvicorn octoforge_web.main:app --reload --reload-dir web/src

run-telegram:
	$(BIN)/python -m octoforge_web.telegram
