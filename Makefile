# =============================================================================
#  claim-triage
#
#  `make help` lists everything. The two paths that matter:
#
#    make up      containers, the way the system is meant to run
#    make dev     everything in one process, no Docker, for working on it
# =============================================================================

SHELL := /bin/sh
COMPOSE ?= docker compose
PY ?= python
VENV := .venv
VENV_PY := $(VENV)/bin/python
ifeq ($(OS),Windows_NT)
	VENV_PY := $(VENV)/Scripts/python.exe
endif
DEV_ENV := scripts/dev.env

.DEFAULT_GOAL := help
.PHONY: help up down logs ps build corpus ingest index-info rebuild-index \
        demo eval eval-judge prompt-compare contracts test lint loadtest \
        loadtest-headless scale clean nuke dev dev-api dev-worker dev-stub \
        trace circuit-open circuit-reset outage recover smoke

help: ## List the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
#  Containers
# -----------------------------------------------------------------------------

up: ## Build and start the whole system
	@test -f .env || (echo "no .env -- copy .env.example and fill in the provider section"; exit 1)
	$(COMPOSE) up --build -d
	@echo
	@echo "  web client   http://localhost:3000"
	@echo "  api docs     http://localhost:8080/docs"
	@echo "  grafana      http://localhost:3001  (anonymous viewer)"
	@echo "  prometheus   http://localhost:9090"
	@echo "  jaeger       http://localhost:16686"
	@echo
	@echo "  next: make corpus && make ingest"

down: ## Stop everything, keep the volumes
	$(COMPOSE) down

build: ## Rebuild the images
	$(COMPOSE) build

ps: ## Show service health
	$(COMPOSE) ps

logs: ## Follow the application logs
	$(COMPOSE) logs -f api worker

scale: ## Run three agent workers (A7)
	$(COMPOSE) up -d --scale worker=3
	$(COMPOSE) ps worker

# -----------------------------------------------------------------------------
#  Corpus (D2)
# -----------------------------------------------------------------------------

corpus: ## Download the insurer's published documents into corpus/raw
	$(COMPOSE) run --rm tools corpus-fetch

ingest: ## Parse, chunk, embed and index the corpus (idempotent)
	$(COMPOSE) run --rm tools ingest

rebuild-index: ## Re-index everything, ignoring content hashes
	$(COMPOSE) run --rm tools ingest --force

index-info: ## Index freshness and composition
	$(COMPOSE) run --rm tools index-info

# -----------------------------------------------------------------------------
#  Evaluation (L2, L3)
# -----------------------------------------------------------------------------

eval: ## Run the fixed evaluation set (needs a real provider)
	$(COMPOSE) run --rm tools eval --out /app/eval/reports/latest.json

eval-judge: ## Evaluation set with the LLM-as-judge scoring pass
	$(COMPOSE) run --rm tools eval --judge --out /app/eval/reports/latest_judged.json

prompt-compare: ## Run both prompt sets over the same set and diff them (L2)
	$(COMPOSE) run --rm tools prompt-compare --sets v1,v2

contracts: ## Write the published JSON Schema for every contract
	$(COMPOSE) run --rm tools export-contracts --outdir /app/contracts

# -----------------------------------------------------------------------------
#  Load test (D6)
# -----------------------------------------------------------------------------

loadtest: ## Locust web UI on :8089
	$(COMPOSE) --profile loadtest up -d locust
	@echo "  locust  http://localhost:8089"

loadtest-headless: ## 3 minutes at 20 concurrent callers, CSV to loadtest/reports
	$(COMPOSE) --profile loadtest run --rm locust \
	  locust -f /app/loadtest/locustfile.py --host http://api:8080 \
	         --headless -u 20 -r 2 -t 3m --csv /app/loadtest/reports/u20

# -----------------------------------------------------------------------------
#  Demonstration helpers
# -----------------------------------------------------------------------------

demo: ## Submit the section 4.1 reference case and print the result
	@scripts/demo.sh

trace: ## Print the full trace for a run:  make trace RUN=run-...
	@test -n "$(RUN)" || (echo "usage: make trace RUN=run-..."; exit 1)
	@curl -fsS http://localhost:8080/v1/runs/$(RUN)/trace | $(PY) -m json.tool

circuit-open: ## Trip the provider circuit breaker in the worker (item 8)
	@curl -fsS -X POST http://localhost:9100/admin/circuit/open || \
	  $(COMPOSE) exec -T worker curl -fsS -X POST http://localhost:9100/admin/circuit/open

circuit-reset: ## Close the circuit breaker again
	@$(COMPOSE) exec -T worker curl -fsS -X POST http://localhost:9100/admin/circuit/reset

outage: ## Make the stub provider return 503 to everything (item 8)
	@curl -fsS -X POST http://localhost:9009/admin/outage

recover: ## End the simulated outage
	@curl -fsS -X POST http://localhost:9009/admin/recover

smoke: ## Exercise every demonstration path against a running system
	@scripts/smoke.sh

# -----------------------------------------------------------------------------
#  Local development, no Docker
# -----------------------------------------------------------------------------

$(VENV_PY):
	$(PY) -m venv $(VENV)
	$(VENV_PY) -m pip install -q --upgrade pip
	$(VENV_PY) -m pip install -q -r requirements-dev.txt

dev: $(VENV_PY) ## Ingest and serve everything in one process on :8080
	@set -a; . $(DEV_ENV); set +a; \
	 PYTHONPATH=. $(VENV_PY) -m claimtriage.cli ingest; \
	 echo; echo "  http://localhost:8080/docs"; echo; \
	 PYTHONPATH=. $(VENV_PY) -m uvicorn claimtriage.api.app:app --reload --port 8080

dev-api: $(VENV_PY) ## API only, queue mode (needs Redis on localhost)
	@set -a; . $(DEV_ENV); set +a; EXECUTION_MODE=queue REDIS_URL=redis://localhost:6379/0 \
	 PYTHONPATH=. $(VENV_PY) -m uvicorn claimtriage.api.app:app --reload --port 8080

dev-worker: $(VENV_PY) ## Worker only, queue mode
	@set -a; . $(DEV_ENV); set +a; EXECUTION_MODE=queue REDIS_URL=redis://localhost:6379/0 \
	 PYTHONPATH=. $(VENV_PY) -m claimtriage.worker.executor

dev-stub: $(VENV_PY) ## The stub provider on :9009
	@set -a; . $(DEV_ENV); set +a; \
	 PYTHONPATH=. $(VENV_PY) -m uvicorn claimtriage.stub.app:app --port 9009

test: $(VENV_PY) ## Run the test suite
	PYTHONUTF8=1 $(VENV_PY) -m pytest

lint: $(VENV_PY) ## Compile-check every module
	$(VENV_PY) -m compileall -q claimtriage tests loadtest && echo "  ok"

# -----------------------------------------------------------------------------
#  Cleaning
# -----------------------------------------------------------------------------

clean: ## Remove local build and report artefacts
	rm -rf .pytest_cache data scratch
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

nuke: ## Remove containers AND volumes. The index and every run are lost.
	$(COMPOSE) down -v
