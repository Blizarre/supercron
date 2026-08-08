# AGENTS.md

Guidelines for AI agents and contributors working in this repository.

## Before every commit

Run the formatter and the full check suite, and fix any failures, before
committing:

```sh
make format
make check
```

- `make format`  -> `ruff format` (auto-format source and tests)
- `make check`   -> `ruff check` (lint), `mypy` (type check), then `pytest`

Only stage and commit once `make format` reports the tree is formatted and
`make check` passes with no lint errors, no type errors, and all tests green.

## About the project

`supercron` is a cron replacement written in Python: a daemon that runs tasks
in isolated persistent containers, records every execution as a TOML file,
fires HTTP callbacks, and exposes a web UI.

## Conventions

- Python 3.11+, standard library only for runtime dependencies.
- Formatting and linting are enforced by ruff (see `pyproject.toml`).
- Tests live in `tests/` and run with pytest.
- Do not add comments to code unless asked.
