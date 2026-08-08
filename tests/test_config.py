from pathlib import Path

import pytest

from supercron.config import ConfigError, load_config, read_config


def test_load_config_defaults():
    cfg = load_config("/cron", {})
    assert cfg.mount_path == "/work"
    assert cfg.timeout == 300
    assert cfg.retention.max_executions == 100
    assert cfg.tasks_path == Path("/cron") / "tasks"


def test_read_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing config"):
        read_config(tmp_path)


def test_read_config_invalid_toml_raises(tmp_path):
    (tmp_path / "config.toml").write_text("image = [unclosed")
    with pytest.raises(ConfigError, match="invalid config"):
        read_config(tmp_path)


def test_load_config_overrides_all_fields():
    cfg = load_config(
        "/cron",
        {
            "image": "alpine:3.19",
            "mount_path": "/data",
            "tasks_dir": "jobs",
            "results_dir": "out",
            "timeout": 60,
            "kill_grace": 5,
        },
    )
    assert cfg.image == "alpine:3.19"
    assert cfg.mount_path == "/data"
    assert cfg.tasks_dir == "jobs"
    assert cfg.results_dir == "out"
    assert cfg.timeout == 60
    assert cfg.kill_grace == 5
    assert cfg.tasks_path == Path("/cron") / "jobs"
    assert cfg.results_path == Path("/cron") / "out"


def test_retention_partial_override_keeps_defaults():
    cfg = load_config("/cron", {"retention": {"max_executions": 7}})
    assert cfg.retention.max_executions == 7
    assert cfg.retention.max_age_days == 30


def test_retention_non_dict_ignored():
    cfg = load_config("/cron", {"retention": "nope"})
    assert cfg.retention.max_executions == 100


def test_read_config_roundtrip(tmp_path):
    (tmp_path / "config.toml").write_text(
        'image = "ubuntu:22.04"\ntimeout = 99\n[retention]\nmax_age_days = 14\n'
    )
    cfg = read_config(tmp_path)
    assert cfg.image == "ubuntu:22.04"
    assert cfg.timeout == 99
    assert cfg.retention.max_age_days == 14
    assert cfg.retention.max_executions == 100
