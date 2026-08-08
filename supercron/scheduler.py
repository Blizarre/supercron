"""Scheduling daemon: timer loop, manual runs, and startup recovery.

Ties together task discovery, the results store, and the docker runner.
Run in its own thread via :meth:`Daemon.start`; use :meth:`Daemon.run_task`
for manual/manual-trigger runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread

from .callbacks import CallbackError, CallbackSender, build_payload
from .config import RootConfig, read_config
from .cron import CronError, CronSchedule
from .records import ExecutionRecord, ResultsStore
from .runner import DockerError, DockerRunner
from .tasks import Task, discover_tasks, utcnow


@dataclass
class ScheduledTask:
    task: Task
    schedule: CronSchedule

    def next_after(self, base: datetime) -> datetime:
        return self.schedule.next_after(base)


class Daemon:
    """Owns the scheduling loop and execution dispatch for tasks."""

    def __init__(
        self,
        cron_dir: str | Path,
        config: RootConfig | None = None,
        poll_interval: float = 1.0,
        store: ResultsStore | None = None,
        runner: DockerRunner | None = None,
        callbacks: CallbackSender | None = None,
    ):
        self.cron_dir = Path(cron_dir)
        self.config = config or read_config(self.cron_dir)
        self.store = store or ResultsStore(self.config.results_path)
        self.runner = runner or DockerRunner(self.config)
        self.callbacks = callbacks if callbacks is not None else CallbackSender()
        self.poll_interval = poll_interval

        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._tasks: dict[str, Task] = {}
        self._scheduled: list[ScheduledTask] = []
        self._running: dict[str, ExecutionRecord] = {}
        self._error_handler: Callable[[Exception], None] | None = None

    # ------------------------------------------------------------ tasks

    def refresh(self) -> None:
        """Re-discover tasks and compile their schedules."""
        tasks = discover_tasks(self.cron_dir, self.config.tasks_dir)
        self._tasks = {task.name: task for task in tasks}
        scheduled: list[ScheduledTask] = []
        for task in tasks:
            if task.schedule:
                try:
                    scheduled.append(
                        ScheduledTask(task, CronSchedule.parse(task.schedule))
                    )
                except CronError as exc:
                    raise CronError(f"task {task.name!r}: {exc}") from exc
        self._scheduled = scheduled

    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def scheduled_tasks(self) -> list[ScheduledTask]:
        return list(self._scheduled)

    def task_by_name(self, name: str) -> Task | None:
        return self._tasks.get(name)

    def running_records(self) -> dict[str, ExecutionRecord]:
        return dict(self._running)

    def set_error_handler(self, handler: Callable[[Exception], None]) -> None:
        self._error_handler = handler

    # ------------------------------------------------------------ execution

    def run_task(self, task: Task, trigger: str = "manual") -> ExecutionRecord:
        """Begin, run, and finalize one execution of ``task``.

        ``trigger`` is one of ``cron``, ``manual``, or ``overridden``.
        """
        previous_killed = self.runner.is_running(task)
        with self._lock:
            rec, log_path = self.store.begin_execution(
                task, trigger=trigger, previous_killed=previous_killed
            )
            self._running[task.name] = rec

        self._fire_callback(task.callbacks.start, rec)
        timeout = task.timeout or self.config.timeout

        def _finish() -> None:
            try:
                result = self.runner.run_execution(task, log_path, timeout=timeout)
                self.store.finalize(
                    rec,
                    return_code=result.exit_code,
                    success=result.success,
                )
            except DockerError as exc:
                self.store.finalize(rec, return_code=None, success=False)
                if self._error_handler:
                    self._error_handler(exc)
            finally:
                end_url = (
                    task.callbacks.end_success
                    if rec.status == "success"
                    else task.callbacks.end_failure
                )
                self._fire_callback(end_url, rec)
                with self._lock:
                    self._running.pop(task.name, None)

        _finish()
        return rec

    def _fire_callback(self, url: str | None, rec: ExecutionRecord) -> None:
        """POST the lifecycle event if a callback URL is configured.

        Best-effort: a failed send is only routed to the error handler.
        """
        if not url:
            return
        try:
            self.callbacks.send(url, build_payload(rec))
        except CallbackError as exc:
            if self._error_handler:
                self._error_handler(exc)

    # ------------------------------------------------------------ recovery

    def recover(self) -> int:
        """Mark stale running records as failed and clean orphan containers."""
        updated = self.store.mark_stale_failed()
        for st in self._scheduled:
            if self.runner.is_running(st.task):
                # An orphan container left behind by a crashed daemon.
                self.runner._stop(st.task)
        return updated

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._loop, name="supercron-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                next_due = self._wait_until_next_due()
                if next_due is not None and not self._stop.is_set():
                    self._dispatch_due(next_due)
            except Exception as exc:
                if self._error_handler:
                    self._error_handler(exc)

    def _wait_until_next_due(self) -> datetime | None:
        while not self._stop.is_set():
            now = utcnow()
            next_due: datetime | None = None
            for st in self._scheduled:
                try:
                    nxt = st.next_after(now)
                except CronError:
                    continue
                if next_due is None or nxt < next_due:
                    next_due = nxt
            if next_due is None:
                # No scheduled tasks; sleep until interrupted.
                self._stop.wait(30)
                continue
            wait = (next_due - now).total_seconds()
            if wait <= 0:
                return next_due
            self._stop.wait(min(wait, self.poll_interval))
        return None

    def _dispatch_due(self, due: datetime) -> None:
        for st in self._scheduled:
            if st.schedule.matches(due):
                self.run_task(st.task, trigger="cron")
