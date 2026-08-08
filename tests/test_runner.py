from pathlib import Path

import pytest

from supercron.config import load_config
from supercron.runner import DockerRunner
from supercron.tasks import Task


def _config(**overrides):
    data = {
        "image": "busybox:latest",
        "mount_path": "/work",
        "timeout": 10,
        "kill_grace": 1,
    }
    data.update(overrides)
    return load_config("/cron", data)


def _task(tmp_path: Path, name: str, script: str) -> Task:
    task_dir = tmp_path / "tasks" / name
    task_dir.mkdir(parents=True)
    start = task_dir / "start.sh"
    start.write_text(script)
    start.chmod(0o755)
    return Task(name=name, task_dir=task_dir, start_script=start)


@pytest.fixture
def runner_cleanup():
    from supercron.runner import DockerRunner

    created: list[str] = []

    def _register(name: str) -> None:
        DockerRunner._cmd("rm", "-f", name)  # clear any stale container
        created.append(name)

    yield _register
    for name in created:
        DockerRunner._cmd("rm", "-f", name)


pytestmark = pytest.mark.skipif(
    not DockerRunner.available(), reason="docker daemon not available"
)


def test_run_success_logs_and_exit_code(tmp_path, runner_cleanup):
    cfg = _config()
    task = _task(tmp_path, "ok", "#!/bin/sh\necho hello-world\nexit 0\n")
    runner_cleanup(task.container_name)
    runner = DockerRunner(cfg)

    created = runner.ensure_container(task)
    assert created is True

    log_path = tmp_path / "results" / "out.log"
    result = runner.run_execution(task, log_path)
    assert result.exit_code == 0
    assert result.success is True
    assert "hello-world" in log_path.read_text()


def test_run_failure_exit_code(tmp_path, runner_cleanup):
    cfg = _config()
    task = _task(tmp_path, "fail", "#!/bin/sh\necho boom >&2\nexit 3\n")
    runner_cleanup(task.container_name)
    runner = DockerRunner(cfg)
    runner.ensure_container(task)

    log_path = tmp_path / "results" / "out.log"
    result = runner.run_execution(task, log_path)
    assert result.exit_code == 3
    assert result.success is False
    assert "boom" in log_path.read_text()


def test_container_is_reused_across_runs(tmp_path, runner_cleanup):
    cfg = _config()
    task = _task(tmp_path, "reuse", "#!/bin/sh\necho run\nexit 0\n")
    runner_cleanup(task.container_name)
    runner = DockerRunner(cfg)

    assert runner.ensure_container(task) is True
    assert runner.container_exists(task) is True
    assert runner.ensure_container(task) is False  # already exists

    log_path = tmp_path / "results" / "out.log"
    assert runner.run_execution(task, log_path).exit_code == 0
    assert runner.run_execution(task, log_path).exit_code == 0


def test_timeout_kills_container(tmp_path, runner_cleanup):
    cfg = _config(timeout=60, kill_grace=1)
    task = _task(tmp_path, "slow", "#!/bin/sh\nsleep 30\nexit 0\n")
    runner_cleanup(task.container_name)
    runner = DockerRunner(cfg, poll_interval=0.1)
    runner.ensure_container(task)

    log_path = tmp_path / "results" / "out.log"
    result = runner.run_execution(task, log_path, timeout=1)
    assert result.timed_out is True
    assert result.exit_code is not None
    assert result.success is False
    assert result.duration < 10


def test_destroy_removes_container(tmp_path, runner_cleanup):
    cfg = _config()
    task = _task(tmp_path, "destroy", "#!/bin/sh\nexit 0\n")
    runner_cleanup(task.container_name)
    runner = DockerRunner(cfg)
    runner.ensure_container(task)
    assert runner.container_exists(task)
    runner.destroy(task)
    assert not runner.container_exists(task)


def test_destroy_missing_container_is_noop(tmp_path):
    cfg = _config()
    task = _task(tmp_path, "ghost", "#!/bin/sh\nexit 0\n")
    runner = DockerRunner(cfg)
    runner.destroy(task)
    assert not runner.container_exists(task)
