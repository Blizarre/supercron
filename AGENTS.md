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

- Python 3.11+, standard library as much as possible for runtime dependencies.
- Formatting and linting are enforced by ruff (see `pyproject.toml`).
- Tests live in `tests/` and run with pytest.
- Comment the code at a high level: add comments about blocks of code if the logic is complex
- Use meaningful method names and variables. Try to make the code self-explanotary.
- Do not add comments when the meaning is already conveyed by the method/names. For instance do NOT do this:
```python
# Fabulate the interface
interface.fabulate()
```
instead do THIS:
```python
interface.fabulate()
```
- Try to extract the low-level code into methods with a meaningful name, but avoid trivial methods that are less than 3 lines long, unless it is repeated many times in the codebase
- Do not return None on failure, throw a dedicated Exception with a message
- Error handling is paramount and in python, exception-based. Do not catch an exception and return None. Instead let the exception propagate and let the caller make a decision
on how to log and handle it.
- In python, do not use getattr() to see if an object has a field. Use interfaces/abstract classes instead to enforce the presence of these fields by the type checker
- Do not raise raw Exceptions, create custom types instead
- Tests must never silently depend on the environment: a test that skips a
  check because a tool or capability is missing hides what is actually
  exercised in a given run, so you never know what was really tested. If a
  test cannot verify its feature, it must fail loudly and clearly instead of
  skipping.