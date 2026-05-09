.PHONY: up down init test lint demo logs psql help install

# Load .env if it exists — copy .env.example to .env and fill in values
-include .env
export

POSTGRES_CONTAINER := genomics-pg
POSTGRES_IMAGE     := postgres:15
POSTGRES_PORT      := 5432

# Env var defaults for local dev (overridden by .env)
export RDS_HOST       ?= localhost
export RDS_DB         ?= genomics_metadata
export RDS_USER       ?= postgres
export RDS_PASSWORD   ?= postgres
export S3_BUCKET_NAME ?= genomics-data-local

help:
	@echo ""
	@echo "  make up     — start local PostgreSQL container"
	@echo "  make down   — stop and remove PostgreSQL container"
	@echo "  make init   — initialize DB schema"
	@echo "  make test   — run test suite"
	@echo "  make lint   — run ruff + black check"
	@echo "  make demo   — run local end-to-end demo (mocked AWS)"
	@echo "  make psql   — open psql shell in the running container"
	@echo "  make logs   — tail container logs"
	@echo ""

up:
	@if docker ps -q -f name=$(POSTGRES_CONTAINER) | grep -q .; then \
		echo "PostgreSQL already running."; \
	elif docker ps -aq -f name=$(POSTGRES_CONTAINER) | grep -q .; then \
		echo "Starting existing container..."; \
		docker start $(POSTGRES_CONTAINER); \
	else \
		echo "Creating and starting PostgreSQL container..."; \
		docker run -d \
			--name $(POSTGRES_CONTAINER) \
			-e POSTGRES_DB=$(RDS_DB) \
			-e POSTGRES_USER=$(RDS_USER) \
			-e POSTGRES_PASSWORD=$(RDS_PASSWORD) \
			-p $(POSTGRES_PORT):5432 \
			$(POSTGRES_IMAGE); \
	fi
	@echo "Waiting for PostgreSQL to be ready..."
	@until docker exec $(POSTGRES_CONTAINER) pg_isready -U $(RDS_USER) -q 2>/dev/null; do sleep 1; done
	@echo "PostgreSQL is ready."

down:
	docker stop $(POSTGRES_CONTAINER) && docker rm $(POSTGRES_CONTAINER)

# Sentinel file — pip only runs when requirements-dev.txt changes
.pip-installed: requirements-dev.txt
	pip install -r requirements-dev.txt
	@touch .pip-installed

install: .pip-installed

init: up .pip-installed
	python db/schema.py --init

test:
	pytest tests/ -v --cov=. --cov-report=term-missing

lint:
	ruff check .
	black --check .

demo: init
	python scripts/demo_ingest.py

psql:
	docker exec -it $(POSTGRES_CONTAINER) psql -U $(RDS_USER) -d $(RDS_DB)

logs:
	docker logs -f $(POSTGRES_CONTAINER)
