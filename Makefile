VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test check run

install:
	python3 -m venv $(VENV)
	$(BIN)/pip install -e "core[dev]" -e "web[dev]"

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

run:
	$(BIN)/uvicorn octoforge_web.main:app --reload --reload-dir web/src
