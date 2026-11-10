# Floorwalker. Run `make setup` once, then `make dev`.
SHELL := /bin/bash
# Needs Python 3.10+. macOS's stock python3 is 3.9, so prefer a versioned interpreter if present.
PY ?= $(shell for p in python3.13 python3.12 python3.11 python3.10 python3; do command -v $$p >/dev/null 2>&1 && { echo $$p; break; }; done)
VENV := backend/.venv
BIN := $(VENV)/bin
# Load backend/.env if it exists (API keys etc.)
ENVLOAD := set -a; [ -f .env ] && . ./.env; set +a;

.PHONY: setup backend web dev test eval eval-llm clean

setup:            ## install backend + frontend dependencies
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ required, found %s. Run: make setup PY=python3.12" % sys.version.split()[0])'
	$(PY) -m venv --clear $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r backend/requirements.txt
	cd web && npm install --no-audit --no-fund
	@[ -f backend/.env ] || cp .env.example backend/.env
	@[ -f web/.env.local ] || cp web/.env.example web/.env.local
	@echo "Setup done. Put your ANTHROPIC_API_KEY in backend/.env, then: make dev"

backend:          ## API + simulator on http://localhost:8000
	cd backend && $(ENVLOAD) .venv/bin/uvicorn app.main:app --port 8000

web:              ## dashboard on http://localhost:3000
	cd web && npm run dev

dev:              ## both, in one terminal (Ctrl-C stops both)
	@trap 'kill 0' INT TERM; $(MAKE) backend & $(MAKE) web & wait

test:             ## unit + integration tests (no API key or network needed)
	cd backend && .venv/bin/python -m pytest -q

eval:             ## incident benchmark, rule-based runbook -> EVALS.md
	cd backend && .venv/bin/python -m evals.run_evals --mode fallback

eval-llm:         ## incident benchmark with the LLM agent (needs ANTHROPIC_API_KEY) -> EVALS.md
	cd backend && $(ENVLOAD) .venv/bin/python -m evals.run_evals --mode llm

clean:
	rm -rf backend/.cache backend/.pytest_cache web/.next
