VENV := .venv
BIN := $(VENV)/bin

.PHONY: install upgrade lint format typecheck test check test-pg db-up db-down db-psql run run-telegram \
	quickstart quickstart-logs quickstart-down bench docs audit

# Test database of the compose postgres service. Separate from the app database
# on purpose: the Postgres store tests drop and recreate the public schema.
#
# The password is read from `.env` rather than hardcoded: a deployment that
# rotated `POSTGRES_PASSWORD` (which it must, once anything reaches the server
# from another host) would otherwise leave `make test-pg` failing to
# authenticate, with nothing to say why. Falls back to the compose default,
# which is what a fresh clone and CI both have.
PG_PASSWORD := $(shell sed -n 's/^POSTGRES_PASSWORD=//p' .env 2>/dev/null)
ifeq ($(strip $(PG_PASSWORD)),)
PG_PASSWORD := octoforge
endif
PG_TEST_URL ?= postgresql+asyncpg://octoforge:$(PG_PASSWORD)@127.0.0.1:5432/octoforge_test

# The local stack: production compose plus the overlay that drops Caddy and
# publishes the app on loopback (no domain, no certificate).
LOCAL_COMPOSE := docker compose -f docker-compose.yml -f docker-compose.local.yml
LOCAL_URL := http://127.0.0.1:8000

install:
	python3 -m venv $(VENV)
	# `venv` seeds whatever pip/setuptools the interpreter shipped with, which on
	# a stock 3.11 is old enough that `make audit` reports advisories against the
	# bootstrap tooling rather than against anything this project depends on.
	$(BIN)/pip install --upgrade pip setuptools
	$(BIN)/pip install -e "core[dev,local-embeddings]" \
		-e server -e surfaces/telegram -e surfaces/console -e surfaces/webui \
		-e "deploy[dev]"

# Bring an existing .venv up to what CI installs. `install` leaves any
# already-satisfied dependency alone, so a linter picked up weeks ago keeps
# disagreeing with CI (which resolves everything fresh on every run) until
# something bumps it.
#
# Two steps on purpose. `core[dev]` is upgraded eagerly — that is where the
# check tooling lives — while `deploy[dev]` only gets the default
# only-if-needed pass: it depends (through the service) on
# octoforge-core[local-embeddings], so an eager resolve there would re-resolve
# torch for a multi-gigabyte download (and off the CPU-only index the
# Dockerfile uses). Version caps from the pyprojects are honored either way,
# unlike a bare `pip install -U ruff`.
upgrade:
	$(BIN)/pip install --upgrade --upgrade-strategy eager -e "core[dev]"
	$(BIN)/pip install --upgrade -e "deploy[dev]"

lint:
	$(BIN)/ruff check core/src core/tests server/src surfaces/*/src deploy/src deploy/tests tools
	$(BIN)/ruff format --check core/src core/tests server/src surfaces/*/src deploy/src deploy/tests tools

format:
	$(BIN)/ruff check --fix core/src core/tests server/src surfaces/*/src deploy/src deploy/tests tools
	$(BIN)/ruff format core/src core/tests server/src surfaces/*/src deploy/src deploy/tests tools

typecheck:
	cd core && ../$(BIN)/mypy
	cd deploy && ../$(BIN)/mypy

test:
	cd core && ../$(BIN)/pytest
	cd deploy && ../$(BIN)/pytest

# Mechanical documentation check: every repository path named in docs/ (and in
# the root markdown files) exists, every internal link resolves. It cannot check
# whether a sentence is true — see docs/CONVENTIONS.md.
docs:
	$(BIN)/python tools/check_docs.py

check: lint typecheck docs test

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

# One command from a fresh clone to a running agent: generate .env (operator
# credential, secret-store key, LLM endpoint), then bring up Postgres + the app
# on loopback. Needs docker and an OpenAI-compatible key, nothing else — no
# virtualenv, no model download.
quickstart:
	python3 tools/quickstart.py
	$(LOCAL_COMPOSE) up -d --wait
	@echo
	@echo "OctoForge is up:"
	@echo "  chat UI          $(LOCAL_URL)/"
	@echo "  operator console $(LOCAL_URL)/admin.html"
	@echo "  API docs         $(LOCAL_URL)/docs"
	@echo "Log in with the credential printed above. Logs: make quickstart-logs"

quickstart-logs:
	$(LOCAL_COMPOSE) logs -f app

quickstart-down:
	$(LOCAL_COMPOSE) down

# Known vulnerabilities in the installed dependency tree. Not part of `check`:
# it needs the network and its verdict changes without the code changing, which
# would make the gate non-deterministic. CI runs it as its own step.
audit:
	$(BIN)/pip-audit --desc --skip-editable

# Latency harness behind the numbers in README.md ("Why it feels fast").
bench:
	$(BIN)/python tools/bench_latency.py

run:
	$(BIN)/uvicorn octoforge_deploy.main:app --reload --reload-dir deploy/src --reload-dir server/src --reload-dir surfaces

run-telegram:
	$(BIN)/python -m octoforge_deploy.telegram_only
