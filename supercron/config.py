"""Loading and schema of the top-level cron/config.toml.

No database: config is a plain TOML file at the cron root.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


@dataclass
class Retention:
    max_executions: int | None = 100
    max_age_days: int | None = 30


@dataclass
class RootConfig:
    cron_dir: str = ""
    image: str = "python:3.12"
    mount_path: str = "/work"
    tasks_dir: str = "tasks"
    results_dir: str = "results"
    timeout: int = 300
    kill_grace: int = 15
    retention: Retention = field(default_factory=Retention)

    @property
    def tasks_path(self) -> Path:
        return Path(self.cron_dir) / self.tasks_dir

    @property
    def results_path(self) -> Path:
        return Path(self.cron_dir) / self.results_dir


def load_config(cron_dir: str | Path, data: dict[str, Any]) -> RootConfig:
    """Build a RootConfig from parsed TOML, applying defaults."""
    cfg = RootConfig()
    cfg.cron_dir = str(cron_dir)

    cfg.image = data.get("image", cfg.image)
    cfg.mount_path = data.get("mount_path", cfg.mount_path)
    cfg.tasks_dir = data.get("tasks_dir", cfg.tasks_dir)
    cfg.results_dir = data.get("results_dir", cfg.results_dir)
    cfg.timeout = int(data.get("timeout", cfg.timeout))
    cfg.kill_grace = int(data.get("kill_grace", cfg.kill_grace))

    if "retention" in data and isinstance(data["retention"], dict):
        ret = data["retention"]
        cfg.retention = Retention(
            max_executions=ret.get("max_executions", cfg.retention.max_executions),
            max_age_days=ret.get("max_age_days", cfg.retention.max_age_days),
        )
    return cfg


def read_config(cron_dir: str | Path) -> RootConfig:
    """Read cron/config.toml and return a RootConfig."""
    cron_dir = Path(cron_dir)
    path = (cron_dir / "config.toml").resolve()
    if not path.exists():
        raise ConfigError(f"missing config.toml at {path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config.toml: {exc}") from exc
    return load_config(cron_dir, data)
