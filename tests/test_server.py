import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from supercron.config import load_config
from supercron.records import ExecutionRecord, ResultsStore
from supercron.runner import ExecutionResult
from supercron.scheduler import Daemon
from supercron.server import Server


class FakeRunner:
    def __init__(self):
        self.destroyed: list[str] = []
        self.created: list[str] = []

    def is_running(self, task):
        return False

    def destroy(self, task):
        self.destroyed.append(task.name)

    def ensure_container(self, task):
        self.created.append(task.name)

    def run_execution(self, task, log_path, timeout=None):
        log_path.write_text(f"log for {task.name}\n")
        return ExecutionResult(exit_code=0)


def build(tmp_path) -> tuple[Server, Daemon, Path, FakeRunner]:
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
    server = Server(daemon)
    server.start()
    return server, daemon, tasks_dir, runner


@pytest.fixture()
def ctx(tmp_path):
    server, daemon, tasks_dir, runner = build(tmp_path)
    yield server, daemon, tasks_dir, runner
    server.stop()


def add_task(tasks_dir: Path, name: str, schedule=None):
    task_dir = tasks_dir / name
    task_dir.mkdir(parents=True)
    (task_dir / "start.sh").write_text("#!/bin/sh\nexit 0\n")
    (task_dir / "start.sh").chmod(0o755)
    cron = f'title = "{name}"\n'
    if schedule:
        cron += f'schedule = "{schedule}"\n'
    (task_dir / "cron.toml").write_text(cron)


def get(server: Server, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}") as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(server: Server, path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_index_lists_task(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "hello")
    daemon.refresh()
    status, body = get(server, "/")
    assert status == 200
    assert "hello" in body and "history" in body


def test_index_defines_action_script(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "hello")
    daemon.refresh()
    _status, body = get(server, "/")
    assert "function action(url)" in body
    assert "function poll()" in body


def test_api_status_never_run(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    status, body = get(server, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data[0]["name"] == "job"
    assert data[0]["status"] == "never_run"


def test_api_status_derives_state(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job", schedule="*/5 * * * *")
    daemon.refresh()
    daemon.store.write_record(
        ExecutionRecord(
            id=1, task="job", status="failure", return_code=3, trigger="manual"
        )
    )
    _status, body = get(server, "/api/status")
    data = json.loads(body)
    assert data[0]["status"] == "failure"
    assert data[0]["schedule"] == "*/5 * * * *"
    assert data[0]["next_run"] is not None


def test_task_page_shows_history_and_log(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    daemon.store.write_record(
        ExecutionRecord(
            id=2, task="job", status="success", return_code=0, trigger="manual"
        )
    )
    (daemon.store.task_dir("job") / "2.log").write_text("hello output\n")
    status, body = get(server, "/task/job")
    assert status == 200
    assert "Run" in body and "job" in body and ">2</td>" in body
    status, body = get(server, "/task/job/log/2")
    assert status == 200
    assert "hello output" in body


def test_unknown_task_page_404(ctx):
    server, _daemon, _tasks, _runner = ctx
    status, _ = get(server, "/task/nope")
    assert status == 404


def test_non_numeric_log_id_404(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    status, _ = get(server, "/task/job/log/not-a-number")
    assert status == 404


def test_post_run_triggers_execution(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    status, body = post(server, "/task/job/run")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    for _ in range(50):
        records = daemon.store.list_records("job")
        if records and records[0].status == "success":
            break
        time.sleep(0.05)
    assert daemon.store.load_record("job", 1).status == "success"


def test_post_run_unknown_task_404(ctx):
    server, _daemon, _tasks, _runner = ctx
    status, _ = post(server, "/task/nope/run")
    assert status == 404


def test_post_reset_unknown_task_404(ctx):
    server, _daemon, _tasks, _runner = ctx
    status, _ = post(server, "/task/nope/reset")
    assert status == 404


def test_post_reset_recreates_container(ctx):
    server, daemon, tasks_dir, runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    status, body = post(server, "/task/job/reset")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert runner.destroyed == ["job"]
    assert runner.created == ["job"]


def test_post_reload_discovers_new_tasks(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "one")
    daemon.refresh()
    assert daemon.task_by_name("two") is None
    add_task(tasks_dir, "two")
    status, body = post(server, "/reload")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert daemon.task_by_name("two") is not None


def test_index_has_reload_button(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    status, body = get(server, "/")
    assert status == 200
    assert "Reload tasks" in body and "/reload" in body


def test_index_shows_status_emoji(ctx):
    server, daemon, tasks_dir, _runner = ctx
    add_task(tasks_dir, "job")
    daemon.refresh()
    daemon.store.write_record(
        ExecutionRecord(id=1, task="job", status="success", return_code=0)
    )
    _status, body = get(server, "/")
    assert "✅ success" in body
    daemon.store.write_record(
        ExecutionRecord(id=2, task="job", status="failure", return_code=1)
    )
    _status, body = get(server, "/")
    assert "🔴 failure" in body


def test_server_roundtrip_many_requests(ctx):
    server, _daemon, _tasks, _runner = ctx
    for _ in range(5):
        status, _ = get(server, "/api/status")
        assert status == 200
