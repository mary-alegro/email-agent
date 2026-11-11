.PHONY: install run serve seed reset-db test eval lint format \
	docker-build docker-test docker-up docker-down docker-logs

HOST ?= 0.0.0.0
PORT ?= 8000
WORKERS ?= 2
REPEATS ?= 1
CASES ?=

install:
	uv sync --extra dev

run:
	uv run uvicorn app.main:app --reload

serve:
	uv run --frozen uvicorn app.main:app --host $(HOST) --port $(PORT) --workers $(WORKERS)

seed:
	uv run python -m src.database.seed

reset-db:
	uv run python -m src.database.seed --reset

test:
	uv run pytest

# Real LLM calls (costs money). E.g. make eval REPEATS=3 CASES=fraud
eval:
	uv run python -m src.evals --repeats $(REPEATS) $(if $(CASES),--cases $(CASES))

lint:
	uv run ruff check app src tests
	uv run ruff format --check app src tests

format:
	uv run ruff check --fix app src tests
	uv run ruff format app src tests

docker-build:
	docker build -t supply-chain-email-agent:latest .

# Build the test image, then run the suite in a container (output shown, exit code kept).
docker-test:
	docker build --target test -t supply-chain-email-agent:test .
	docker run --rm supply-chain-email-agent:test

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app
