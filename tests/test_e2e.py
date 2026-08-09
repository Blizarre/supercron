"""End-to-end tests wiring the daemon, results store, and web server together."""

import json
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from supercron.config import load_config
from supercron.runner import ExecutionResult
from supercron.scheduler import Daemon
from supercron.server import Server


class FakeRunner:
    def __init__(self):
        self.runs: list[str] = []
        self.containers: set[str] = set()

    def is_running(self, task):
        return False

    def stop(self, task):
        pass

    def ensure_container(self, task):
        self.containers.add(task.container_name)

    def destroy(self, task):
        self.containers.discard(task.container_name)

    def list_containers(self):
        return set(self.containers)

    def remove_container(self, name):
        self.containers.discard(name)

    def run_execution(self, task, log_path, timeout=None):
        self.runs.append(task.name)
        log_path.write_text(f"ran {task.name}\n")
        return ExecutionResult(exit_code=0)


def build(tmp_path, retention=None) -> tuple[Daemon, Path, FakeRunner]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    (tmp_path / "config.toml").write_text('image = "busybox:latest"\n')
    data: dict[str, object] = {"tasks_dir": "tasks"}
    if retention:
        data["retention"] = {"max_executions": retention}
    cfg = load_config(tmp_path, data)
    runner = FakeRunner()
    daemon = Daemon(
        str(tmp_path),
        config=cfg,
        poll_interval=3600,
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


def test_e2e_run_queried_via_server(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "job")
    daemon.refresh()
    server = Server(daemon)
    server.start()
    try:
        task = daemon.task_by_name("job")
        assert task is not None
        daemon.run_task(task, trigger="manual")
        assert runner.runs == ["job"]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/api/task/job"
        ) as resp:
            data = json.loads(resp.read())
        assert data[0]["status"] == "success"
        assert data[0]["id"] == 1
    finally:
        server.stop()


def test_e2e_scheduled_and_manual_share_history(tmp_path):
    daemon, tasks_dir, _runner = build(tmp_path)
    add_task(tasks_dir, "job", schedule="*/5 * * * *")
    daemon.refresh()
    task = daemon.task_by_name("job")
    assert task is not None
    daemon._dispatch_due(datetime.now(UTC).replace(minute=5, second=0, microsecond=0))
    for _ in range(500):
        if len(daemon.store.list_records("job")) == 1:
            break
        time.sleep(0.01)
    daemon.run_task(task, trigger="manual")
    recs = daemon.store.list_records("job")
    assert [r.trigger for r in recs] == ["cron", "manual"]


def test_e2e_prune_applies_after_runs(tmp_path):
    daemon, tasks_dir, _runner = build(tmp_path, retention=2)
    add_task(tasks_dir, "job")
    daemon.refresh()
    task = daemon.task_by_name("job")
    assert task is not None
    for _ in range(5):
        daemon.run_task(task, trigger="manual")
    recs = daemon.store.list_records("job")
    assert len(recs) == 2
    assert recs[-1].id == 5
    leftover = {p.name for p in daemon.store.task_dir("job").iterdir()}
    assert all(f"{i}.toml" not in leftover for i in (1, 2, 3))


def test_e2e_recover_removes_orphan_container(tmp_path):
    daemon, tasks_dir, runner = build(tmp_path)
    add_task(tasks_dir, "job")
    daemon.refresh()
    task = daemon.task_by_name("job")
    assert task is not None
    runner.containers = {task.container_name, "supercron-leftover"}
    daemon.recover()
    assert "supercron-leftover" not in runner.containers
    assert task.container_name in runner.containers
