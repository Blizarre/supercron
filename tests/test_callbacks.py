import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from supercron.callbacks import CallbackError, CallbackSender, build_payload
from supercron.config import load_config
from supercron.records import ExecutionRecord, ResultsStore
from supercron.runner import ExecutionResult
from supercron.scheduler import Daemon


class FakeRunner:
    def __init__(self):
        self.success = True
        self.raise_docker_error = False

    def is_running(self, task):
        return False

    def run_execution(self, task, log_path, timeout=None):
        if self.raise_docker_error:
            raise RuntimeError
        return ExecutionResult(exit_code=0 if self.success else 7)


class FakeCallbacks:
    def __init__(self):
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.fail_first = False

    def send(self, url, payload):
        if self.fail_first:
            self.fail_first = False
            raise CallbackError("boom")
        self.sent.append((url, payload))


def build(tmp_path) -> tuple[Daemon, Path, FakeRunner, FakeCallbacks]:
    (tmp_path / "config.toml").write_text('image = "busybox:latest"\n')
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True)
    cfg = load_config(tmp_path, {"tasks_dir": "tasks"})
    runner = FakeRunner()
    callbacks = FakeCallbacks()
    daemon = Daemon(
        str(tmp_path),
        config=cfg,
        poll_interval=3600,
        store=ResultsStore(cfg.results_path),
        runner=runner,
        callbacks=callbacks,
    )
    return daemon, tasks_dir, runner, callbacks


def add_task(
    tasks_dir: Path,
    name: str,
    start="http://monitor/start",
    ok="http://monitor/ok",
    fail="http://monitor/fail",
):
    task_dir = tasks_dir / name
    task_dir.mkdir(parents=True)
    (task_dir / "start.sh").write_text("#!/bin/sh\nexit 0\n")
    (task_dir / "start.sh").chmod(0o755)
    cron = f'title = "{name}"\n[callbacks]\n'
    if start:
        cron += f'start = "{start}"\n'
    if ok:
        cron += f'end_success = "{ok}"\n'
    if fail:
        cron += f'end_failure = "{fail}"\n'
    (task_dir / "cron.toml").write_text(cron)


def test_build_payload_shape():
    rec = ExecutionRecord(
        id=3,
        task="t",
        status="success",
        started_at=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 8, 10, 5, tzinfo=UTC),
        return_code=0,
    )
    payload = build_payload(rec)
    assert payload["task"] == "t"
    assert payload["execution_id"] == 3
    assert payload["status"] == "success"
    assert payload["return_code"] == 0
    assert payload["started_at"].startswith("2026-08-08T10:00")
    assert payload["ended_at"].startswith("2026-08-08T10:05")


def test_build_payload_running_omits_nulls():
    rec = ExecutionRecord(
        id=1, task="t", started_at=datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    )
    payload = build_payload(rec)
    assert payload["status"] == "running"
    assert "return_code" not in payload
    assert "ended_at" not in payload


def test_send_posts_json_with_content_type():
    sender = CallbackSender()
    fake_resp = MagicMock()
    fake_resp.read.return_value = b""
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        sender.send("http://monitor/ok", {"task": "t"})
    req = urlopen.call_args.args[0]
    assert req.method == "POST"
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data) == {"task": "t"}


def test_send_raises_callback_error_on_failure():
    sender = CallbackSender()
    with (
        patch("urllib.request.urlopen", side_effect=OSError("refused")),
        pytest.raises(CallbackError, match="http://monitor/ok"),
    ):
        sender.send("http://monitor/ok", {"task": "t"})


def test_send_noop_on_empty_url():
    sender = CallbackSender()
    with patch("urllib.request.urlopen") as urlopen:
        sender.send("", {"task": "t"})
    urlopen.assert_not_called()


def test_run_task_fires_start_and_end_success(tmp_path):
    daemon, tasks_dir, _runner, callbacks = build(tmp_path)
    add_task(tasks_dir, "t")
    daemon.refresh()
    daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    urls = [url for url, _ in callbacks.sent]
    assert urls == ["http://monitor/start", "http://monitor/ok"]
    assert callbacks.sent[0][1]["status"] == "running"
    assert callbacks.sent[1][1]["status"] == "success"
    assert callbacks.sent[1][1]["return_code"] == 0
    assert callbacks.sent[1][0] == "http://monitor/ok"


def test_run_task_fires_end_failure(tmp_path):
    daemon, tasks_dir, runner, callbacks = build(tmp_path)
    runner.success = False
    add_task(tasks_dir, "t")
    daemon.refresh()
    daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    urls = [url for url, _ in callbacks.sent]
    assert urls == ["http://monitor/start", "http://monitor/fail"]
    assert callbacks.sent[1][1]["status"] == "failure"
    assert callbacks.sent[1][1]["return_code"] == 7


def test_no_callbacks_configured_sends_nothing(tmp_path):
    daemon, tasks_dir, _runner, callbacks = build(tmp_path)
    add_task(tasks_dir, "t", start=None, ok=None, fail=None)
    daemon.refresh()
    daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    assert callbacks.sent == []


def test_callback_failure_routed_to_error_handler(tmp_path):
    daemon, tasks_dir, _runner, callbacks = build(tmp_path)
    add_task(tasks_dir, "t")
    daemon.refresh()
    errors: list[CallbackError] = []
    daemon.set_error_handler(errors.append)
    callbacks.fail_first = True
    rec = daemon.run_task(daemon.task_by_name("t"), trigger="manual")
    assert rec.status == "success"
    assert len(errors) == 1
    assert isinstance(errors[0], CallbackError)
    # The second (end) callback still fires.
    assert callbacks.sent[-1][0] == "http://monitor/ok"
