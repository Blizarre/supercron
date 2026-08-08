.PHONY: lint format check typecheck test

# Lint the codebase with ruff.
lint:
	ruff check supercron tests

# Auto-format the codebase with ruff.
format:
	ruff format supercron tests

# Run static type checking with mypy.
typecheck:
	python3 -m mypy supercron tests

# Run the full check suite (lint + typecheck + tests).
check:
	ruff check supercron tests
	python3 -m mypy supercron tests
	python3 -m pytest -q

# Run tests only.
test:
	python3 -m pytest -q
