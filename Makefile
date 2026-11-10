# CourtControl — local (docker-free) development & test targets.
# All heavy lifting lives in scripts/test_env.sh; run `make help` for usage.

SHELL  := /bin/bash
SCRIPT := bash scripts/test_env.sh

# Optional test labels and extra `manage.py test` flags, e.g.:
#   make test TEST=apps.bookings.tests.test_services ARGS="-v 2"
TEST ?=
ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help setup run stop seed seed-reset deps test test-fast smoke db-start db-stop db-status db-shell \
        db-reset clean install-postgres install-redis

help: ## Show this help
	@echo "Usage: make <target> [TEST=<labels>] [ARGS=\"<manage.py test flags>\"]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Env overrides: RUN_ADDR (default 127.0.0.1:8000), PG_PORT (54329), REDIS_PORT (63790),"
	@echo "               PG_BIN, REDIS_BIN, PYTHON, KEEP_DB_RUNNING=1,"
	@echo "               SEED=0 (run without seed data), SMOKE_BASE_URL (smoke target),"
	@echo "               PG_OS_USER / PG_ROOT_BASE (when run as root)"

setup: ## Install Python deps and init/start the local PostgreSQL cluster
	@$(SCRIPT) setup

run: ## Run the stack: PostgreSQL + Redis + Django (:8000) + Celery worker/beat
	@$(SCRIPT) run

stop: ## Stop PostgreSQL and Redis if left running
	@$(SCRIPT) stop

seed: ## Load dev seed data: clubs, courts, players, bookings (run does it too)
	@$(SCRIPT) seed

seed-reset: ## Delete and recreate all seed data
	@$(SCRIPT) seed --reset

deps: ## Create .venv and install requirements.txt (skipped if unchanged)
	@$(SCRIPT) deps

test: ## Run the test suite (TEST=apps.bookings ARGS="-v 2")
	@$(SCRIPT) test $(TEST) $(ARGS)

smoke: ## Smoke-test the `make run` stack over HTTP (starts/stops it if not running)
	@$(SCRIPT) smoke $(ARGS)

test-fast: ## Run tests reusing the test DB, in parallel
	@$(SCRIPT) test $(TEST) --keepdb --parallel $(ARGS)

db-start: ## Start the local PostgreSQL cluster (initializes on first run)
	@$(SCRIPT) db-start

db-stop: ## Stop the local PostgreSQL cluster
	@$(SCRIPT) db-stop

db-status: ## Show PostgreSQL / Redis / virtualenv status
	@$(SCRIPT) db-status

db-shell: ## Open psql on the local database
	@$(SCRIPT) db-shell

db-reset: ## Stop PostgreSQL/Redis and delete their data
	@$(SCRIPT) db-reset

clean: ## Remove database/Redis data and .venv
	@$(SCRIPT) clean

install-postgres: ## Install PostgreSQL with the OS package manager (sudo)
	@$(SCRIPT) install-postgres

install-redis: ## Install Redis (needed for Celery in `make run`)
	@$(SCRIPT) install-redis
