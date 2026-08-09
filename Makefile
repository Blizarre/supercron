.PHONY: lint format check typecheck test e2e

# Lint the codebase with ruff.
lint:
	ruff check supercron tests e2e

# Auto-format the codebase with ruff.
format:
	ruff format supercron tests e2e

# Run static type checking with mypy.
typecheck:
	python3 -m mypy supercron tests e2e

# Run the full check suite (lint + typecheck + fast tests).
check:
	ruff check supercron tests e2e
	python3 -m mypy supercron tests e2e
	python3 -m pytest -q

# Run tests only.
test:
	python3 -m pytest -q

# Run the real-Docker end-to-end suite (~10 min; requires Docker).
e2e:
	python3 -m pytest e2e -q
