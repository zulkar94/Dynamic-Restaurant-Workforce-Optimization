.PHONY: help install dev seed run test cov lint fmt type security audit check docker-build docker-up clean

PY ?= python3

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "%-14s %s\n", $$1, $$2}'

install: ## runtime deps only
	$(PY) -m pip install -r requirements.txt

dev: ## runtime + dev tooling
	$(PY) -m pip install -e ".[dev]"

seed: ## generate 180 days of demo demand and 28 staff
	$(PY) scripts/seed_demo.py --days 180 --employees 28 --reset

run: ## start the API with autoreload on :8000
	uvicorn app.main:app --reload --port 8000

test: ## run the suite
	$(PY) -m pytest

cov: ## suite with coverage report
	$(PY) -m pytest --cov=app --cov-report=term-missing --cov-report=xml

lint: ## ruff check
	ruff check app tests scripts

fmt: ## ruff autofix + format
	ruff check --fix app tests scripts && ruff format app tests scripts

type: ## mypy
	mypy app

security: ## bandit static analysis
	bandit -q -c pyproject.toml -r app

audit: ## dependency CVE scan
	pip-audit -r requirements.txt

check: lint type security test ## everything CI runs

docker-build:
	docker build -t rwo-api:1.0.0 .

docker-up:
	docker compose up --build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml *.db artifacts
