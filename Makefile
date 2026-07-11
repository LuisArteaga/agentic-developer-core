.PHONY: verify setup secret-scan lint format-check type-check test

verify: lint format-check type-check test

setup:
	@echo "Installing pre-commit git hooks..."
	pre-commit install
	@echo "Git hooks installed successfully."

secret-scan:
	python3 scripts/secret_scan.py

test:
	python -m pytest

lint:
	python -m ruff check orchestrator scripts

format-check:
	python -m ruff format --check orchestrator scripts

type-check:
	python -m mypy orchestrator scripts

