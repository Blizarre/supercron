from pathlib import Path

import pytest

from supercron.tasks import Task, discover_tasks, utcnow


def _mk_task(tmp_path, name, with_cron=True):
    task = tmp_path / "tasks" / name
    task.mkdir(parents=True)
    (task / "start.sh").write_text("#!/bin/bash\n")
    (task / "start.sh").chmod(0o755)
    if with_cron:
        (task / "cron.toml").write_text('title = "' + name + '"')
    return task


def test_discover_tasks_and_cron_toml(tmp_path):
    _mk_task(tmp_path, "task1")
    (tmp_path / "tasks" / "task1" / "cron.toml").write_text(
        'title = "T1"\nschedule = "*/5 * * * *"\n'
        '[callbacks]\nstart = "https://m/x"\nend_success = "https://m/ok"\n'
    )
    tasks = discover_tasks(tmp_path)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.name == "task1"
    assert t.title == "T1"
    assert t.schedule == "*/5 * * * *"
    assert t.callbacks.start == "https://m/x"
    assert t.callbacks.end_success == "https://m/ok"
    assert t.callbacks.end_failure is None
    assert t.container_name == "supercron-task1"


def test_discover_missing_tasks_dir_returns_empty(tmp_path):
    assert discover_tasks(tmp_path) == []


def test_discover_ignores_files_and_dirs_without_start(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "notes.txt").write_text("x")
    bad = tmp_path / "tasks" / "bad"
    bad.mkdir()
    (bad / "cron.toml").write_text("")
    _mk_task(tmp_path, "good")
    names = [t.name for t in discover_tasks(tmp_path)]
    assert names == ["good"]


def test_discover_invalid_cron_toml_then_name_default(tmp_path):
    _mk_task(tmp_path, "a", with_cron=False)
    tasks = discover_tasks(tmp_path)
    assert tasks[0].name == "a"
    assert tasks[0].title == "a"
    assert tasks[0].schedule is None
    assert tasks[0].callbacks.start is None


def test_discover_sorted_order(tmp_path):
    for n in ["b", "a", "c"]:
        _mk_task(tmp_path, n)
    assert [t.name for t in discover_tasks(tmp_path)] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("simple", "supercron-simple"),
        ("with space", "supercron-with_space"),
        ("UPPER_case-1", "supercron-UPPER_case-1"),
        ("dots.and-dashes", "supercron-dots.and-dashes"),
        ("weird/name!", "supercron-weird_name_"),
    ],
)
def test_container_name_sanitization(name, expected):
    t = Task(name=name, task_dir=Path("."), start_script=Path("start.sh"))
    assert t.container_name == expected


def test_utcnow_is_timezone_aware():
    now = utcnow()
    assert now.tzinfo is not None
