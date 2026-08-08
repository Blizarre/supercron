import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from supercron.config import Retention
from supercron.records import ExecutionRecord, RecordNotFound, ResultsStore
from supercron.tasks import Task, utcnow


def _task(tmp_path, name="task1"):
    return Task(
        name=name,
        task_dir=tmp_path / "tasks" / name,
        start_script=tmp_path / "tasks" / name / "start.sh",
    )


def test_begin_finalize_roundtrip(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, log = store.begin_execution(_task(tmp_path), "manual")
    assert rec.status == "running"
    assert rec.id == 1
    with store.open_log(log) as fh:
        fh.write("hello\n")
    store.finalize(rec, return_code=0, success=True)

    loaded = store.load_record("task1", 1)
    assert loaded.status == "success"
    assert loaded.return_code == 0
    assert loaded.started_at and loaded.ended_at
    assert log.exists()
    assert log.read_text() == "hello\n"


def test_atomic_id_assignment_concurrent(tmp_path):
    store = ResultsStore(tmp_path / "results")
    t = _task(tmp_path)

    def run(_):
        r, _log = store.begin_execution(t, "cron")
        store.finalize(r, return_code=0, success=True)
        return r.id

    with ThreadPoolExecutor(16) as ex:
        ids = list(ex.map(run, range(100)))
    assert len(set(ids)) == 100
    assert len(store.list_records("task1")) == 100


def test_record_dump_is_valid_toml(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, _ = store.begin_execution(_task(tmp_path), "cron")
    record_path = Path(store.task_dir("task1") / f"{rec.id}.toml")
    data = tomllib.loads(record_path.read_text())
    assert data["id"] == 1
    assert data["status"] == "running"
    assert "extra" not in data


def test_begin_leaves_running_record_without_end(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, _ = store.begin_execution(_task(tmp_path), "cron")
    assert rec.status == "running"
    assert rec.ended_at is None
    assert rec.return_code is None
    assert rec.started_at is not None


def test_finalize_failure_sets_status_and_code(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, _ = store.begin_execution(_task(tmp_path), "cron")
    store.finalize(rec, return_code=3, success=False)
    assert rec.status == "failure"
    assert rec.return_code == 3
    assert rec.ended_at is not None
    loaded = store.load_record("task1", rec.id)
    assert loaded.status == "failure"
    assert loaded.return_code == 3


def test_ids_increment_across_multiple_starts(tmp_path):
    store = ResultsStore(tmp_path / "results")
    t = _task(tmp_path)
    ids = [store.begin_execution(t, "cron")[0].id for _ in range(3)]
    assert ids == [1, 2, 3]


def test_previous_killed_and_trigger_roundtrip(tmp_path):
    store = ResultsStore(tmp_path / "results")
    t = _task(tmp_path)
    rec, _ = store.begin_execution(t, "overridden", previous_killed=True)
    loaded = store.load_record("task1", rec.id)
    assert loaded.trigger == "overridden"
    assert loaded.previous_killed is True


def test_open_log_appends(tmp_path):
    store = ResultsStore(tmp_path / "results")
    _rec, log = store.begin_execution(_task(tmp_path), "cron")
    for line in ("one\n", "two\n"):
        with store.open_log(log) as fh:
            fh.write(line)
    assert log.read_text() == "one\ntwo\n"


def test_load_record_missing_raises(tmp_path):
    store = ResultsStore(tmp_path / "results")
    with pytest.raises(RecordNotFound):
        store.load_record("task1", 99)


def test_list_records_skips_corrupt_and_meta(tmp_path):
    store = ResultsStore(tmp_path / "results")
    _rec, _ = store.begin_execution(_task(tmp_path), "cron")
    task_dir = store.task_dir("task1")
    (task_dir / "2.toml").write_text("this is not toml [[[")
    (task_dir / "meta.toml").write_text('foo = "bar"')
    (task_dir / "3.toml").write_text("id = 3, task = 1")  # invalid int for task
    recs = store.list_records("task1")
    assert [r.id for r in recs] == [1]
    assert len(recs) == 1


def test_list_records_sorted_by_id(tmp_path):
    store = ResultsStore(tmp_path / "results")
    t = _task(tmp_path)
    recs = [store.begin_execution(t, "cron")[0] for _ in range(5)]
    ids = [r.id for r in store.list_records("task1")]
    assert ids == sorted([r.id for r in recs])


def test_atomic_write_leaves_no_temp_files(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, _ = store.begin_execution(_task(tmp_path), "cron")
    leftovers = [p.name for p in store.task_dir("task1").iterdir()]
    assert not any(name.endswith(".tmp") or ".toml." in name for name in leftovers)
    assert f"{rec.id}.toml" in leftovers


def test_to_dict_omits_empty_fields():
    rec = ExecutionRecord(id=5, task="t")
    d = rec.to_dict()
    assert "trigger" not in d
    assert "ended_at" not in d
    assert "extra" not in d
    assert d["id"] == 5


def test_dump_is_valid_toml_and_roundtrips_datetime(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec, _ = store.begin_execution(_task(tmp_path), "cron")
    store.finalize(rec, return_code=0, success=True)
    data = tomllib.loads((store.task_dir("task1") / f"{rec.id}.toml").read_text())
    assert datetime.fromisoformat(data["started_at"]).tzinfo is not None
    assert data["status"] == "success"
    assert data["return_code"] == 0
    assert data["log_file"].endswith(".log")


def test_write_record_without_lock_persists(tmp_path):
    store = ResultsStore(tmp_path / "results")
    rec = ExecutionRecord(id=42, task="task1", status="running")
    store.write_record(rec)
    loaded = store.load_record("task1", 42)
    assert loaded.id == 42
    assert loaded.task == "task1"


def test_prune_by_execution_count(tmp_path):
    store = ResultsStore(tmp_path / "results")
    for i in range(1, 6):
        rec = ExecutionRecord(id=i, task="task1", status="success")
        store.write_record(rec)
    store.prune(Retention(max_executions=3, max_age_days=None))
    assert [r.id for r in store.list_records("task1")] == [3, 4, 5]


def test_prune_by_age(tmp_path):
    store = ResultsStore(tmp_path / "results")
    old = ExecutionRecord(
        id=1,
        task="task1",
        status="failure",
        started_at=utcnow() - timedelta(days=60),
        ended_at=utcnow() - timedelta(days=59),
    )
    new = ExecutionRecord(
        id=2,
        task="task1",
        status="success",
        started_at=utcnow(),
        ended_at=utcnow(),
    )
    store.write_record(old)
    store.write_record(new)
    store.prune(Retention(max_executions=None, max_age_days=30))
    assert [r.id for r in store.list_records("task1")] == [2]


def test_prune_removes_log_files_too(tmp_path):
    store = ResultsStore(tmp_path / "results")
    for i in (1, 2, 3):
        store.write_record(ExecutionRecord(id=i, task="task1", status="success"))
        (store.task_dir("task1") / f"{i}.log").write_text("x\n")
    store.prune(Retention(max_executions=1, max_age_days=None))
    assert not (store.task_dir("task1") / "1.log").exists()
    assert (store.task_dir("task1") / "3.log").exists()


def test_prune_missing_results_dir_is_noop(tmp_path):
    store = ResultsStore(tmp_path / "results")
    assert store.prune(Retention(max_executions=5)) == 0
