"""Scheduling daemon: timer loop, manual runs, and startup recovery.

Ties together task discovery, the results store, and the docker runner.
Run in its own thread via :meth:`Daemon.start`; use :meth:`Daemon.run_task`
for manual/manual-trigger runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread

from .callbacks import CallbackError, CallbackSender, build_payload
from .config import RootConfig, read_config
from .cron import CronError, CronSchedule
from .records import ExecutionRecord, ResultsStore
from .runner import DockerRunner
from .tasks import Task, discover_tasks, utcnow

log = logging.getLogger("supercron.scheduler")


@dataclass
class ScheduledTask:
    task: Task
    schedule: CronSchedule

    def next_after(self, base: datetime) -> datetime:
        return self.schedule.next_after(base)


class TaskNotFound(Exception):
    """Raised when an action references a task that does not exist."""


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
        self.cron_dir = Path(cron_dir).resolve()
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
        log.info("discovered %d task(s)", len(tasks))
        for task in tasks:
            log.info(
                "  %-25s schedule=%-18s timeout=%s",
                task.name,
                task.schedule or "-",
                task.timeout or self.config.timeout,
            )

    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def scheduled_tasks(self) -> list[ScheduledTask]:
        return list(self._scheduled)

    def task_by_name(self, name: str) -> Task | None:
        return self._tasks.get(name)

    def trigger_task(self, name: str) -> None:
        """Start a manual run of ``name`` in a background thread.

        Raises :class:`TaskNotFound` if no such task exists. The run
        completes asynchronously, so callers should poll for status.
        """
        task = self._task_or_raise(name)
        Thread(target=self.run_task, args=(task, "manual"), daemon=True).start()

    def reset_task(self, name: str) -> None:
        """Destroy and recreate the persistent container for ``name``.

        Raises :class:`TaskNotFound` if no such task exists.
        """
        task = self._task_or_raise(name)
        self.runner.destroy(task)
        self.runner.ensure_container(task)

    def _task_or_raise(self, name: str) -> Task:
        task = self._tasks.get(name)
        if task is None:
            raise TaskNotFound(f"no task named {name!r}")
        return task

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
        log.info(
            "task %s: %s run #%d starting (previous_killed=%s)",
            task.name,
            trigger,
            rec.id,
            previous_killed,
        )

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
            except Exception as exc:
                log.exception("task %s: run #%d failed", task.name, rec.id)
                self.store.finalize(rec, return_code=None, success=False)
                if self._error_handler:
                    self._error_handler(exc)
            finally:
                self.store.prune(self.config.retention)
                duration = 0.0
                if rec.started_at and rec.ended_at:
                    duration = (rec.ended_at - rec.started_at).total_seconds()
                log.info(
                    "task %s: %s run #%d finished: status=%s return_code=%s (%.1fs)",
                    task.name,
                    trigger,
                    rec.id,
                    rec.status,
                    rec.return_code,
                    duration,
                )
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
        """Mark stale records failed, stop orphan runs, and drop stray containers.

        Returns the number of stale records updated.
        """
        updated = self.store.mark_stale_failed()
        log.info("startup recovery: marked %d stale record(s) failed", updated)
        for st in self._scheduled:
            if self.runner.is_running(st.task):
                # An orphan container left behind by a crashed daemon.
                log.info(
                    "stopping orphaned running container %s",
                    st.task.container_name,
                )
                self.runner.stop(st.task)
        self._prune_orphan_containers()
        return updated

    def _prune_orphan_containers(self) -> None:
        """Remove ``supercron-*`` containers that no longer correspond to a task."""
        known = {task.container_name for task in self.tasks()}
        for name in self.runner.list_containers():
            if name not in known:
                self.runner.remove_container(name)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._loop, name="supercron-scheduler", daemon=True
        )
        self._thread.start()
        log.info("scheduler started")

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

    def _earliest_next(self, base: datetime) -> datetime | None:
        """Return the soonest next run across all tasks, or None if none."""
        next_due: datetime | None = None
        for st in self._scheduled:
            try:
                nxt = st.next_after(base)
            except CronError:
                continue
            if next_due is None or nxt < next_due:
                next_due = nxt
        return next_due

    def _wait_until_next_due(self) -> datetime | None:
        # Compute the due time once, then sleep until the clock reaches it.
        # It is not recomputed each poll, otherwise next_after(now) always
        # yields a strictly-future instant and the due run would be skipped.
        next_due = self._earliest_next(utcnow())
        while not self._stop.is_set():
            if next_due is None:
                # No scheduled tasks; sleep until interrupted.
                self._stop.wait(30)
                next_due = self._earliest_next(utcnow())
                continue
            wait = (next_due - utcnow()).total_seconds()
            if wait <= 0:
                return next_due
            self._stop.wait(min(wait, self.poll_interval))
        return None

    def _dispatch_due(self, due: datetime) -> None:
        for st in self._scheduled:
            if st.schedule.matches(due):
                self.run_task(st.task, trigger="cron")
