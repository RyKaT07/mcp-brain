.PHONY: audit install dev test lint

audit:
	python3 -m pip_audit

install:
	pip install -e ".[dev,integrations,scrapers]"

dev:
	uvicorn mcp_brain.server:app --reload

test:
	pytest

lint:
	ruff check .
