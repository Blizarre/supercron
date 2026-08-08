"""Allow running the daemon via ``python -m supercron``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
