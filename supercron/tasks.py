"""Task discovery and per-task cron.toml parsing."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


class TaskError(Exception):
    """Raised for an invalid task directory."""


@dataclass
class TaskCallbacks:
    start: str | None = None
    end_success: str | None = None
    end_failure: str | None = None


@dataclass
class Task:
    name: str
    task_dir: Path
    start_script: Path
    title: str = ""
    schedule: str | None = None
    timeout: int | None = None
    callbacks: TaskCallbacks = field(default_factory=TaskCallbacks)

    @property
    def container_name(self) -> str:
        # "supercron-<task>" -> container name
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in self.name)
        return f"supercron-{safe}"


def _parse_cron_toml(path: Path, task: Task) -> None:
    if not path.exists():
        return  # cron.toml is optional; title/schedule default
    data = tomllib.loads(path.read_text())
    task.title = data.get("title", task.name)
    task.schedule = data.get("schedule", task.schedule)
    task.timeout = data.get("timeout", task.timeout)
    callbacks = data.get("callbacks", {})
    if isinstance(callbacks, dict):
        task.callbacks.start = callbacks.get("start")
        task.callbacks.end_success = callbacks.get("end_success")
        task.callbacks.end_failure = callbacks.get("end_failure")


def discover_tasks(cron_dir: str | Path, tasks_dir: str = "tasks") -> list[Task]:
    """Scan cron/tasks/*/ for valid tasks (each must contain start.sh)."""
    base = Path(cron_dir) / tasks_dir
    tasks: list[Task] = []
    if not base.is_dir():
        return tasks

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        start = entry / "start.sh"
        if not start.is_file():
            # Not a valid task; ignore silently (could also log).
            continue
        task = Task(
            name=entry.name,
            task_dir=entry,
            start_script=start,
            title=entry.name,
        )
        _parse_cron_toml(entry / "cron.toml", task)
        tasks.append(task)
    return tasks


def utcnow() -> datetime:
    return datetime.now(UTC)
