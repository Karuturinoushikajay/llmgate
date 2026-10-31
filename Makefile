PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install test lint format typecheck run migrate ci up load

BASE_URL ?= http://127.0.0.1:8000
API_KEY ?=
MODEL ?= gpt-4o-mini
CONCURRENCY ?= 4
REQUESTS ?= 20

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[dev]"

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

run:
	$(BIN)/uvicorn llmgate.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	$(BIN)/alembic upgrade head

ci: lint typecheck test

up:
	docker compose up --build

load:
	$(BIN)/python scripts/load_baseline.py --base-url $(BASE_URL) --api-key $(API_KEY) --model $(MODEL) --concurrency $(CONCURRENCY) --requests $(REQUESTS)
