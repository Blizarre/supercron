"""Execution records: per-execution TOML + log files stored under results/.

Atomic writes + per-task locking so id assignment is safe without a database.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any

from .config import Retention
from .tasks import Task, utcnow


class RecordNotFound(Exception):
    """Raised when an execution record does not exist."""


@dataclass
class ExecutionRecord:
    id: int
    task: str
    status: str = "running"  # running | success | failure
    started_at: datetime | None = None
    ended_at: datetime | None = None
    return_code: int | None = None
    log_file: str = ""
    trigger: str = ""  # cron | manual | overridden
    previous_killed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("started_at", "ended_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return {k: v for k, v in d.items() if v is not None and v != ""}


def _record_from_dict(data: dict[str, Any]) -> ExecutionRecord:
    rec = ExecutionRecord(
        id=int(data["id"]),
        task=data["task"],
        status=data.get("status", "running"),
        started_at=_parse_dt(data.get("started_at")),
        ended_at=_parse_dt(data.get("ended_at")),
        return_code=data.get("return_code"),
        log_file=data.get("log_file", ""),
        trigger=data.get("trigger", ""),
        previous_killed=bool(data.get("previous_killed", False)),
    )
    return rec


def _parse_dt(value: Any) -> datetime | None:
    if value is None or not value:
        return None
    return datetime.fromisoformat(value)


def _dump_toml(rec: ExecutionRecord) -> str:
    lines = [
        "\n".join(
            f"{k} = {_toml_value(v)}" for k, v in rec.to_dict().items() if v is not None
        )
    ]
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return f'"{v}"'


class ResultsStore:
    """Owns results/<task>/, assigning ids and persisting records atomically."""

    def __init__(self, results_dir: str | Path):
        self.results_dir = Path(results_dir)

    def task_dir(self, task_name: str) -> Path:
        d = self.results_dir / task_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def existing_task_dir(self, task_name: str) -> Path | None:
        d = self.results_dir / task_name
        return d if d.is_dir() else None

    def _lock_path(self, task_name: str) -> Path:
        return self.task_dir(task_name) / ".lock"

    def _lock(self, task_name: str) -> IO[str]:
        path = self._lock_path(task_name)
        lock_fh = path.open("a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        return lock_fh

    @staticmethod
    def _unlock(lock_fh: IO[str]) -> None:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()

    def begin_execution(
        self, task: Task, trigger: str, previous_killed: bool = False
    ) -> tuple[ExecutionRecord, Path]:
        """Create a new execution atomically under the per-task lock.

        id assignment and the record write happen inside the same critical
        section so concurrent callers never observe a stale directory.
        """
        lock_fh = self._lock(task.name)
        try:
            ids = (
                int(p.stem)
                for p in self.task_dir(task.name).glob("*.toml")
                if p.name != "meta.toml"
            )
            eid = max(ids, default=0) + 1
            rec = ExecutionRecord(
                id=eid,
                task=task.name,
                started_at=utcnow(),
                trigger=trigger,
                previous_killed=previous_killed,
            )
            log_path = self.task_dir(task.name) / f"{eid}.log"
            rec.log_file = str(log_path)
            self._atomic_write(rec)
            return rec, log_path
        finally:
            self._unlock(lock_fh)

    def open_log(self, log_path: Path) -> IO[str]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return log_path.open("a")

    def _atomic_write(self, rec: ExecutionRecord) -> None:
        """Persist a record atomically (temp file + os.replace)."""
        task_dir = self.task_dir(rec.task)
        fd, tmp = tempfile.mkstemp(dir=task_dir, prefix=f"{rec.id}.toml.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(_dump_toml(rec))
            os.replace(tmp, task_dir / f"{rec.id}.toml")
        except BaseException:
            os.unlink(tmp)
            raise

    def write_record(self, rec: ExecutionRecord) -> None:
        self._atomic_write(rec)

    def finalize(
        self, rec: ExecutionRecord, return_code: int | None, success: bool
    ) -> None:
        rec.ended_at = utcnow()
        rec.return_code = return_code
        rec.status = "success" if success else "failure"
        self.write_record(rec)

    def mark_stale_failed(self) -> int:
        """Mark records left 'running' by a crashed daemon as failed.

        Returns the number of records updated.
        """
        updated = 0
        if not self.results_dir.is_dir():
            return updated
        for task_dir in sorted(p for p in self.results_dir.iterdir() if p.is_dir()):
            task_name = task_dir.name
            for rec in self.list_records(task_name):
                if rec.status == "running" and rec.ended_at is None:
                    rec.status = "failure"
                    rec.ended_at = utcnow()
                    rec.return_code = None
                    self.write_record(rec)
                    updated += 1
        return updated

    def load_record(self, task_name: str, eid: int) -> ExecutionRecord:
        task_dir = self.existing_task_dir(task_name)
        if task_dir is None:
            raise RecordNotFound(f"no record for task {task_name!r} id {eid}")
        path = task_dir / f"{eid}.toml"
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return _record_from_dict(data)

    def list_records(self, task_name: str) -> list[ExecutionRecord]:
        task_dir = self.existing_task_dir(task_name)
        if task_dir is None:
            return []
        paths = [p for p in task_dir.glob("*.toml") if p.name != "meta.toml"]
        recs = []
        for p in paths:
            try:
                with p.open("rb") as fh:
                    recs.append(_record_from_dict(tomllib.load(fh)))
            except (OSError, KeyError, tomllib.TOMLDecodeError):
                continue
        recs.sort(key=lambda r: r.id)
        return recs

    def prune(self, retention: Retention) -> int:
        """Delete records (and their logs) outside the retention policy.

        A record is pruned when it is older than the newest
        ``max_executions`` records or its end/start time precedes
        ``max_age_days``. A ``None`` limit disables that constraint.
        Returns the number of files removed.
        """
        removed = 0
        if not self.results_dir.is_dir():
            return removed
        for task_dir in sorted(p for p in self.results_dir.iterdir() if p.is_dir()):
            removed += self._prune_task(task_dir, retention)
        return removed

    def _prune_task(self, task_dir: Path, retention: Retention) -> int:
        records = self.list_records(task_dir.name)
        doomed: set[int] = set()
        if retention.max_executions is not None:
            overflow = len(records) - retention.max_executions
            if overflow > 0:
                doomed.update(r.id for r in records[:overflow])
        if retention.max_age_days is not None:
            cutoff = utcnow() - timedelta(days=retention.max_age_days)
            for rec in records:
                ended = rec.ended_at or rec.started_at
                if ended is not None and ended < cutoff:
                    doomed.add(rec.id)
        removed = 0
        for eid in doomed:
            (task_dir / f"{eid}.toml").unlink(missing_ok=True)
            (task_dir / f"{eid}.log").unlink(missing_ok=True)
            removed += 1
        return removed
