import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supercron.config import load_config
from supercron.cron import CronError, CronSchedule
from supercron.records import ExecutionRecord, ResultsStore
from supercron.runner import DockerError, ExecutionResult
from supercron.scheduler import Daemon, TaskNotFound


class FakeRunner:
    def __init__(self):
        self.calls: list[str] = []
        self.alive = False
        self.containers: set[str] = set()

    def is_running(self, task):
        return self.alive

    def stop(self, task):
        self.alive = False

    def list_containers(self):
        return set(self.containers)

    def remove_container(self, name):
        self.containers.discard(name)

    def run_execution(self, task, log_path, timeout=None):
        self.calls.append(task.name)
        log_path.write_text("fake\n")
        return ExecutionResult(exit_code=0)


class ConcurrentRunner(FakeRunner):
    """Blocks every execution until released so runs provably overlap."""

    def __init__(self):
        super().__init__()
        self.started: set[str] = set()
        self.release = threading.Event()

    def run_execution(self, task, log_path, timeout=None):
        self.started.add(task.name)
        self.release.wait(5)
        self.calls.append(task.name)
        log_path.write_text("ran\n")
        return ExecutionResult(exit_code=0)


class OverlapRunner(FakeRunner):
    """Simulates a lingering run that a later tick overlap-kills."""

    def __init__(self):
        super().__init__()
        self.active = 0
        self.first_started = threading.Event()
        self.first_kill = threading.Event()

    def is_running(self, task):
        return self.active > 0

    def stop(self, task):
        self.first_kill.set()

    def run_execution(self, task, log_path, timeout=None):
        if self.active == 0:
            self.active = 1
            self.first_started.set()
            if self.first_kill.wait(5):
                log_path.write_text("killed\n")
                return ExecutionResult(exit_code=137)
            log_path.write_text("ok\n")
            return ExecutionResult(exit_code=0)
        self.stop(task)
        log_path.write_text("ok\n")
        return ExecutionResult(exit_code=0)


def build(tmp_path) -> tuple[Daemon, Path, FakeRunner]:
    """Create config + tasks dir and return a daemon wired to a fake runner."""
    (tmp_path / "config.toml").write_text('image = "busybox:latest"\n')
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    cfg = load_config(tmp_path, {"tasks_dir": "tasks"})
    runner = FakeRunner()
    daemon = Daemon(
        str(tmp_path),
        config=cfg,
        poll_interval=3600,
        store=ResultsStore(cfg.results_path),
        runner=runner,
    )
    return daemon, tasks_dir, runner


def add_task(tasks_dir: Path, name: str, schedule=None):
    task_dir = tasks_dir / name
    task_dir.mkdir(parents=True)
    (task_dir / "start.sh").write_text("#!/bin/sh\nexit 0\n")
    (task_dir / "start.sh").chmod(0o755)
    cron = f'title = "{name}"\n'
    if schedule:
        cron += f'schedule = "{schedule}"\n'
    (task_dir / "cron.toml").write_text(cron)


def wait_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout:.1f}s")


def test_daemon_resolves_relative_cron_dir(tmp_path, monkeypatch):
    add_task(tmp_path / "tasks", "t")
    # Create a minimal config so read_config() succeeds.
    (tmp_path / "config.toml").write_text('image = "busybox:latest"\n')
    monkeypatch.chdir(tmp_path)
    daemon = Daemon(".")
    assert daemon.cron_dir == tmp_path.resolve()
    assert daemon.config.results_path == (tmp_path / "results").resolve()
    daemon.refresh()
    task = daemon.task_by_name("t")
    assert task is not None
    assert task.task_dir.is_absolute()


def test_daemon_refresh_compiles_schedule(tmp_path):
    daemon, tasks_dir, _runner = build(tmp_path)
    add_task(tasks_dir, "t", schedule="*/5 * * * *")
    daemon.refresh()
    assert len(daemon.scheduled_tasks()) == 1
    assert daemon.scheduled_tasks()[0].schedule == CronSchedule.parse("*/5 * * * *")


def test_daemon_refresh_invalid_schedule_raises(tmp_path):
    daemon, tasks_dir, _runner = build(tmp_path)
    add_task(tasks_dir, "bad", schedule="not a cron")
    with pytest.raises(CronError):
        daemon.refresh()


def test_run_task_creates_success_record(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t")
    daemon.refresh()
    rec = daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    assert runner.calls == ["t"]
    assert rec.status == "success"
    assert rec.trigger == "manual"
    assert rec.ended_at is not None
    assert daemon.store.load_record("t", rec.id).status == "success"


def test_run_task_marks_failure_on_runner_error(tmp_path):
    class BoomRunner(FakeRunner):
        def run_execution(self, task, log_path, timeout=None):
            raise DockerError("boom")

    daemon, tasks_dir, _runner = build(tmp_path)
    daemon.runner = BoomRunner()
    add_task(tasks_dir, "t")
    daemon.refresh()
    rec = daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    assert rec.status == "failure"
    assert rec.return_code is None


def test_run_task_finalizes_on_unexpected_error(tmp_path):
    class CrashRunner(FakeRunner):
        def run_execution(self, task, log_path, timeout=None):
            raise RuntimeError("boom")

    daemon, tasks_dir, _runner = build(tmp_path)
    daemon.runner = CrashRunner()
    add_task(tasks_dir, "t")
    daemon.refresh()
    errors: list[Exception] = []
    daemon.set_error_handler(errors.append)
    rec = daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    assert rec.status == "failure"
    assert rec.return_code is None
    assert daemon.store.load_record("t", rec.id).status == "failure"
    assert len(errors) == 1


def test_trigger_task_unknown_raises(tmp_path):
    daemon, _tasks_dir, _runner = build(tmp_path)
    daemon.refresh()
    with pytest.raises(TaskNotFound):
        daemon.trigger_task("missing")


def test_reset_task_unknown_raises(tmp_path):
    daemon, _tasks_dir, _runner = build(tmp_path)
    daemon.refresh()
    with pytest.raises(TaskNotFound):
        daemon.reset_task("missing")


def test_dispatch_runs_due_scheduled_tasks(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t", schedule="*/5 * * * *")
    daemon.refresh()
    daemon._dispatch_due(datetime(2026, 8, 8, 10, 5, tzinfo=UTC))
    wait_until(lambda: runner.calls == ["t"])
    daemon._dispatch_due(datetime(2026, 8, 8, 10, 6, tzinfo=UTC))
    time.sleep(0.05)  # dispatch is async; a wrong match would have shown up
    assert runner.calls == ["t"]


def test_cron_dispatch_runs_due_tasks_concurrently(tmp_path):
    """Cron ticks at the same minute run in parallel instead of serializing."""
    runner = ConcurrentRunner()
    daemon, tasks_dir, _runner = build(tmp_path)
    daemon.runner = runner
    add_task(tasks_dir, "a", schedule="* * * * *")
    add_task(tasks_dir, "b", schedule="* * * * *")
    daemon.refresh()

    def finalized(name):
        recs = daemon.store.list_records(name)
        return len(recs) == 1 and recs[-1].status != "running"

    daemon._dispatch_due(datetime(2026, 8, 8, 10, 5, tzinfo=UTC))
    wait_until(lambda: runner.started == {"a", "b"})
    runner.release.set()
    wait_until(lambda: finalized("a"))
    wait_until(lambda: finalized("b"))


def test_cron_dispatch_overlaps_lingering_run(tmp_path):
    """A running task is killed and restarted when its next tick is due."""
    runner = OverlapRunner()
    daemon, tasks_dir, _runner = build(tmp_path)
    daemon.runner = runner
    add_task(tasks_dir, "t", schedule="* * * * *")
    daemon.refresh()

    def both_finalized():
        recs = daemon.store.list_records("t")
        return len(recs) == 2 and all(r.status != "running" for r in recs)

    daemon._dispatch_due(datetime(2026, 8, 8, 10, 5, tzinfo=UTC))
    assert runner.first_started.wait(5)
    daemon._dispatch_due(datetime(2026, 8, 8, 10, 6, tzinfo=UTC))
    wait_until(both_finalized)
    first = daemon.store.load_record("t", 1)
    second = daemon.store.load_record("t", 2)
    assert first.status == "failure"
    assert first.return_code == 137
    assert second.status == "success"
    assert second.previous_killed is True


def test_wait_until_next_due_returns_when_clock_reaches_due(tmp_path, monkeypatch):
    """Regression: the scheduling loop must hand a due time to the dispatcher.

    Previously next_due was recomputed from the current clock each poll, and
    because next_after() is strictly-after, wait never reached <= 0, so due
    tasks were never dispatched on schedule.
    """
    daemon, tasks_dir, _runner = build(tmp_path)
    add_task(tasks_dir, "t", schedule="*/5 * * * *")
    daemon.refresh()

    now = [datetime(2026, 8, 9, 10, 0, 0, tzinfo=UTC)]
    monkeypatch.setattr("supercron.scheduler.utcnow", lambda: now[0])

    class FakeStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            now[0] += timedelta(seconds=seconds)

    daemon._stop = FakeStop()
    due = daemon._wait_until_next_due()
    assert due == datetime(2026, 8, 9, 10, 5, 0, tzinfo=UTC)


def test_scheduler_loop_dispatches_due_task(tmp_path, monkeypatch):
    """End-to-end check that the loop actually runs the due task."""
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t", schedule="*/5 * * * *")
    daemon.refresh()

    now = [datetime(2026, 8, 9, 10, 0, 0, tzinfo=UTC)]
    monkeypatch.setattr("supercron.scheduler.utcnow", lambda: now[0])

    class FakeStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            now[0] += timedelta(seconds=seconds)
            # Trip the stop only once the clock is past the due time, so the
            # 10:05 dispatch runs before the loop exits.
            if now[0] > datetime(2026, 8, 9, 10, 5, 0, tzinfo=UTC):
                self.stopped = True

    daemon._stop = FakeStop()
    daemon._loop()
    wait_until(lambda: runner.calls == ["t"])


def test_recover_marks_stale_running_records(tmp_path):
    daemon, tasks_dir, _runner = build(tmp_path)
    add_task(tasks_dir, "t")
    daemon.refresh()
    store = ResultsStore(tmp_path / "results")
    store.write_record(ExecutionRecord(id=1, task="t", status="running"))
    assert daemon.recover() == 1
    loaded = store.load_record("t", 1)
    assert loaded.status == "failure"
    assert loaded.ended_at is not None


def test_recover_stops_orphan_running_container(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t", schedule="* * * * *")
    daemon.refresh()
    runner.alive = True
    daemon.recover()
    assert runner.alive is False


def test_recover_removes_orphan_containers(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t")
    daemon.refresh()
    task = daemon.task_by_name("t")
    assert task is not None
    runner.containers = {task.container_name, "supercron-gone"}
    daemon.recover()
    assert "supercron-gone" not in runner.containers
    assert task.container_name in runner.containers


def test_recover_does_not_prune_containers_before_tasks_discovered(tmp_path):
    """Regression: recover() must not wipe containers if no tasks are known.

    recover() runs at daemon startup; when it runs before tasks are
    discovered the task list is empty, so orphan pruning would delete every
    existing container and lose all persisted state across restarts.
    """
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "t")
    assert daemon.tasks() == []  # not refreshed yet
    runner.containers = {"supercron-t", "supercron-stale"}
    daemon.recover()
    assert runner.containers == {"supercron-t", "supercron-stale"}


def test_run_task_end_to_end_with_docker(tmp_path):
    from supercron.runner import DockerRunner

    if not DockerRunner.available():
        pytest.skip("docker daemon not available")

    (tmp_path / "config.toml").write_text('image = "busybox:latest"\ntimeout = 10\n')
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    add_task(tasks_dir, "e2e")
    cfg = load_config(tmp_path, {"tasks_dir": "tasks"})
    daemon = Daemon(str(tmp_path), config=cfg, poll_interval=3600)
    daemon.refresh()
    task = daemon.task_by_name("e2e")
    assert task is not None
    try:
        rec = daemon.run_task(task, trigger="manual")
        assert rec.status == "success"
        log_path = daemon.store.task_dir(task.name) / f"{rec.id}.log"
        assert log_path.exists()
    finally:
        daemon.runner.destroy(task)
