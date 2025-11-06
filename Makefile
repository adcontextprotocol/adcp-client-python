.PHONY: help format lint typecheck test regenerate-schemas pre-push ci-local clean install-dev

# Detect Python and use venv if available
PYTHON := $(shell if [ -f .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PIP := $(shell if [ -f .venv/bin/pip ]; then echo .venv/bin/pip; else echo pip3; fi)
PYTEST := $(shell if [ -f .venv/bin/pytest ]; then echo .venv/bin/pytest; else echo pytest; fi)
BLACK := $(shell if [ -f .venv/bin/black ]; then echo .venv/bin/black; else echo black; fi)
RUFF := $(shell if [ -f .venv/bin/ruff ]; then echo .venv/bin/ruff; else echo ruff; fi)
MYPY := $(shell if [ -f .venv/bin/mypy ]; then echo .venv/bin/mypy; else echo mypy; fi)

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install-dev: ## Install package in development mode with dev dependencies
	$(PIP) install -e ".[dev]"

format: ## Format code with black
	$(BLACK) src/ tests/ scripts/
	@echo "✓ Code formatted successfully"

lint: ## Run linter (ruff) on source code
	$(RUFF) check src/ tests/
	@echo "✓ Linting passed"

typecheck: ## Run type checker (mypy) on source code
	$(MYPY) src/adcp/
	@echo "✓ Type checking passed"

test: ## Run test suite with coverage
	$(PYTEST) tests/ -v --cov=src/adcp --cov-report=term-missing
	@echo "✓ All tests passed"

test-fast: ## Run tests without coverage (faster)
	$(PYTEST) tests/ -v
	@echo "✓ All tests passed"

test-generation: ## Run only code generation tests
	$(PYTEST) tests/test_code_generation.py -v
	@echo "✓ Code generation tests passed"

regenerate-schemas: ## Download latest schemas and regenerate models
	@echo "Downloading latest schemas..."
	$(PYTHON) scripts/sync_schemas.py
	@echo "Fixing schema references..."
	$(PYTHON) scripts/fix_schema_refs.py
	@echo "Generating Pydantic models..."
	$(PYTHON) scripts/generate_models_simple.py
	@echo "✓ Schemas regenerated successfully"

validate-generated: ## Validate generated code (syntax and imports)
	@echo "Validating generated code..."
	@$(PYTHON) -m py_compile src/adcp/types/generated.py
	@echo "✓ Generated code validation passed"

pre-push: format lint typecheck test validate-generated ## Run all checks before pushing (format, lint, typecheck, test, validate)
	@echo ""
	@echo "================================"
	@echo "✓ All pre-push checks passed!"
	@echo "================================"
	@echo ""
	@echo "Safe to push to remote."

ci-local: lint typecheck test validate-generated ## Run CI checks locally (without formatting)
	@echo ""
	@echo "================================"
	@echo "✓ All CI checks passed!"
	@echo "================================"

clean: ## Clean generated files and caches
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf .coverage
	rm -rf htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✓ Cleaned all generated files and caches"

build: ## Build distribution packages
	python -m build
	@echo "✓ Distribution packages built"

# Development workflow commands

quick-check: lint test-fast ## Quick check (lint + fast tests) for rapid iteration
	@echo "✓ Quick check passed"

full-check: pre-push ## Alias for pre-push (full check before committing)

# Schema workflow

check-schema-drift: ## Check if schemas are out of sync with upstream
	@echo "Checking for schema drift..."
	@$(PYTHON) scripts/sync_schemas.py
	@$(PYTHON) scripts/fix_schema_refs.py
	@$(PYTHON) scripts/generate_models_simple.py
	@if git diff --exit-code src/adcp/types/generated.py schemas/cache/; then \
		echo "✓ Schemas are up-to-date"; \
	else \
		echo "✗ Schemas are out of date!"; \
		echo "Run: make regenerate-schemas"; \
		git diff src/adcp/types/generated.py; \
		exit 1; \
	fi

# Help users understand what to run
.DEFAULT_GOAL := help
