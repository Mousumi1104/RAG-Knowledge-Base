# RAG Chatbot — one-command dev runner
#
# Local stack:
#   - Postgres+pgvector via backend/scripts/pg_local.sh on 127.0.0.1:54329
#   - Backend FastAPI (uvicorn) on 127.0.0.1:8000  (env from backend/.env)
#   - Frontend Next.js 14 on 127.0.0.1:3000
#
# Most backend recipes pin the local env inline so they work regardless of the
# caller's environment. `make backend` loads backend/.env via uvicorn --env-file.

# --- Shared config ----------------------------------------------------------
BACKEND_DIR := backend
FRONTEND_DIR := frontend
PY := ./venv/bin
APP_DB_URL := postgresql+psycopg://raguser@127.0.0.1:54329/ragdb
TEST_DB_URL := postgresql+psycopg://raguser@127.0.0.1:54329/ragdb_test
LOCAL_ENV := VECTOR_DB=pgvector EMBEDDING_PROVIDER=local LLM_PROVIDER=local

.DEFAULT_GOAL := help

.PHONY: help install db-up db-stop db-status migrate backend frontend frontend-prod \
        test e2e lint format verify up

help: ## List available targets
	@echo "RAG Chatbot — make targets:"
	@echo "  help        Show this help (default)"
	@echo "  install     Install backend (pip) and frontend (npm) deps"
	@echo "  db-up       Start local Postgres+pgvector (pg_local.sh up)"
	@echo "  db-stop     Stop local Postgres"
	@echo "  db-status   Show local Postgres status"
	@echo "  migrate     Apply Alembic migrations to ragdb"
	@echo "  backend     Run FastAPI (uvicorn --reload) on 127.0.0.1:8000"
	@echo "  frontend    Run Next.js dev server on port 3000 (slow on /mnt drvfs)"
	@echo "  frontend-prod  Build + serve frontend in production mode (fast on WSL)"
	@echo "  test        Run backend pytest against ragdb_test"
	@echo "  e2e         Run live end-to-end smoke test"
	@echo "  lint        Lint backend (ruff+black) and frontend (eslint)"
	@echo "  format      Auto-format backend and frontend"
	@echo "  verify      Full-stack verification (scripts/verify_stack.sh)"
	@echo "  up          db-up + migrate, then print run instructions"

install: ## Install backend + frontend dependencies
	cd $(BACKEND_DIR) && $(PY)/pip install -r requirements.txt -r requirements-dev.txt
	cd $(FRONTEND_DIR) && npm install

db-up: ## Start local Postgres+pgvector
	bash $(BACKEND_DIR)/scripts/pg_local.sh up

db-stop: ## Stop local Postgres
	bash $(BACKEND_DIR)/scripts/pg_local.sh stop

db-status: ## Show local Postgres status
	bash $(BACKEND_DIR)/scripts/pg_local.sh status

migrate: ## Apply Alembic migrations to ragdb
	cd $(BACKEND_DIR) && DATABASE_URL=$(APP_DB_URL) $(PY)/alembic upgrade head

backend: ## Run FastAPI (uvicorn --reload) on 127.0.0.1:8000
	cd $(BACKEND_DIR) && $(PY)/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --env-file .env

frontend: ## Run Next.js dev server on port 3000 (slow on /mnt drvfs; prefer frontend-prod)
	cd $(FRONTEND_DIR) && npm run dev

frontend-prod: ## Build + serve frontend in production mode on port 3000 (fast on WSL/drvfs)
	cd $(FRONTEND_DIR) && npm run build && npm run start

test: ## Run backend pytest against ragdb_test
	cd $(BACKEND_DIR) && DATABASE_URL=$(TEST_DB_URL) $(LOCAL_ENV) $(PY)/python -m pytest -q

e2e: ## Run live end-to-end smoke test
	bash $(BACKEND_DIR)/scripts/e2e_smoke.sh

lint: ## Lint backend (ruff+black) and frontend (eslint)
	cd $(BACKEND_DIR) && $(PY)/ruff check app tests && $(PY)/black --check app tests
	cd $(FRONTEND_DIR) && npm run lint

format: ## Auto-format backend and frontend
	cd $(BACKEND_DIR) && $(PY)/black app tests && $(PY)/ruff check --fix app tests
	cd $(FRONTEND_DIR) && npm run format

verify: ## Full-stack verification
	bash scripts/verify_stack.sh

up: db-up migrate ## Bring up DB + migrate, then print run instructions
	@echo ""
	@echo "=============================================================="
	@echo " Stack base is up: Postgres running and migrations applied."
	@echo ""
	@echo " Now start the app servers in two separate terminals:"
	@echo "   Terminal 1:  make backend     # FastAPI on http://127.0.0.1:8000"
	@echo "   Terminal 2:  make frontend    # Next.js  on http://localhost:3000"
	@echo ""
	@echo " Then open http://localhost:3000 in your browser."
	@echo "=============================================================="
