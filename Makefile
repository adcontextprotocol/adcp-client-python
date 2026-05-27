.PHONY: help check-uv bootstrap format lint lint-all typecheck typecheck-all test test-type-checks check-type-ignore-contract regenerate-schemas pre-push ci-local clean install-dev check-schema-drift

# Detect Python and use venv if available
PYTHON := $(shell if [ -f .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PIP := $(shell if [ -f .venv/bin/pip ]; then echo .venv/bin/pip; else echo pip3; fi)
PYTEST := $(shell if [ -f .venv/bin/pytest ]; then echo .venv/bin/pytest; else echo pytest; fi)
BLACK := $(shell if [ -f .venv/bin/black ]; then echo .venv/bin/black; else echo black; fi)
RUFF := $(shell if [ -f .venv/bin/ruff ]; then echo .venv/bin/ruff; else echo ruff; fi)
MYPY := $(shell if [ -f .venv/bin/mypy ]; then echo .venv/bin/mypy; else echo mypy; fi)
UV := uv

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install-dev: ## Install with pip dev extra only; bootstrap is preferred for contributors
	$(PIP) install -e ".[dev]"

check-uv:
	@command -v $(UV) >/dev/null || { \
		echo "uv is required for bootstrap and pre-commit hooks. Install it from https://docs.astral.sh/uv/"; \
		exit 1; \
	}

bootstrap: check-uv ## Install uv-managed dev deps and pre-commit hooks
	$(UV) run --extra dev --group dev pre-commit install
	$(UV) run --extra dev --group dev pre-commit install --hook-type commit-msg

format: ## Format code with black (excludes generated files)
	$(BLACK) src/ tests/ scripts/
	@echo "✓ Code formatted successfully (_generated.py excluded via pyproject.toml)"

lint: ## Run linter (ruff) on source code
	$(RUFF) check src/
	@echo "✓ Linting passed"

lint-all: ## Run linter (ruff) on source and tests
	$(RUFF) check src/ tests/
	@echo "✓ Source and test linting passed"

typecheck: ## Run type checker (mypy) on source code
	$(MYPY) src/adcp/
	@echo "✓ Type checking passed"

typecheck-all: typecheck test-type-checks check-type-ignore-contract ## Run all type-check contracts
	@echo "✓ All type-check contracts passed"

test: ## Run test suite with coverage
	$(PYTEST) tests/ -v --cov=src/adcp --cov-report=term-missing
	@echo "✓ All tests passed"

test-fast: ## Run tests without coverage (faster)
	$(PYTEST) tests/ -v
	@echo "✓ All tests passed"

test-type-checks: ## Run adopter-pattern type-check suite (mypy --strict, zero type: ignore allowed)
	$(MYPY) --strict tests/type_checks/
	@echo "✓ Adopter type-checks passed"

check-type-ignore-contract: ## Fail if adopter type-check fixtures use type: ignore suppressions
	$(PYTHON) scripts/check_type_ignore_contract.py
	@echo "✓ Adopter type-check fixtures contain no type: ignore suppressions"

test-generation: ## Run only code generation tests
	$(PYTEST) tests/test_code_generation.py -v
	@echo "✓ Code generation tests passed"

regenerate-registry: ## Regenerate registry types from OpenAPI spec
	@echo "Generating registry types from OpenAPI spec..."
	$(PYTHON) scripts/generate_registry_types.py
	@echo "✓ Registry types regenerated"

regenerate-schemas: ## Download latest schemas and skills from bundle, then regenerate models
	@echo "Downloading latest schemas and skills..."
	$(PYTHON) scripts/sync_schemas.py
	@echo "Fixing schema references..."
	$(PYTHON) scripts/fix_schema_refs.py
	@echo "Bundling schemas into package..."
	$(PYTHON) scripts/bundle_schemas.py
	@echo "Generating Pydantic models..."
	$(PYTHON) scripts/generate_types.py
	@echo "Consolidating exports..."
	$(PYTHON) scripts/consolidate_exports.py
	@echo "Generating ergonomic coercion..."
	$(PYTHON) scripts/generate_ergonomic_coercion.py
	@echo "✓ Schemas regenerated successfully"

validate-generated: ## Validate generated code (syntax and imports)
	@echo "Validating generated code..."
	@$(PYTHON) -m py_compile src/adcp/types/_generated.py
	@echo "✓ Generated code validation passed"

pre-push: format lint typecheck-all test validate-generated ## Run all checks before pushing (format, lint, typecheck, test, validate)
	@echo ""
	@echo "================================"
	@echo "✓ All pre-push checks passed!"
	@echo "================================"
	@echo ""
	@echo "Safe to push to remote."

ci-local: lint typecheck-all test validate-generated ## Run core CI checks locally (without formatting)
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
	$(PYTHON) scripts/bundle_schemas.py
	python -m build
	@echo "✓ Distribution packages built"

# Development workflow commands

quick-check: lint test-fast ## Quick check (lint + fast tests) for rapid iteration
	@echo "✓ Quick check passed"

full-check: pre-push ## Alias for pre-push (full check before committing)

# Schema workflow

check-schema-drift: ## Check if schemas are out of sync with upstream
	@echo "Checking for schema drift..."
	@$(PYTHON) scripts/sync_schemas.py --no-skills
	@$(PYTHON) scripts/fix_schema_refs.py
	@$(PYTHON) scripts/generate_types.py
	@if git diff --exit-code src/adcp/types/_generated.py schemas/cache/; then \
		echo "✓ Schemas are up-to-date"; \
	else \
		echo "✗ Schemas are out of date!"; \
		echo "Run: make regenerate-schemas"; \
		git diff src/adcp/types/_generated.py; \
		exit 1; \
	fi

# Help users understand what to run
.DEFAULT_GOAL := help
