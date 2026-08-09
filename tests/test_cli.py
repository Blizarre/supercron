import signal
import threading
import time
from typing import Any

import pytest

from supercron import cli


@pytest.fixture
def cron_dir(tmp_path: Any) -> str:
    (tmp_path / "config.toml").write_text('image = "busybox:latest"\n')
    return str(tmp_path)


def test_main_refreshes_before_recover(cron_dir: str, monkeypatch: Any) -> None:
    """Regression: containers must not be pruned before tasks are known.

    recover() prunes orphan containers using the discovered task list; if it
    runs before refresh() the list is empty and every persisted container
    would be deleted on each daemon start.
    """
    order: list[str] = []
    handlers: dict[int, Any] = {}

    class FakeDaemon:
        def __init__(self, cron_dir: str, config: object | None = None) -> None:
            assert config is not None

        def refresh(self) -> None:
            order.append("refresh")

        def recover(self) -> None:
            order.append("recover")

        def start(self) -> None:
            order.append("start")

        def stop(self) -> None:
            order.append("stop")

    class FakeServer:
        port: int = 0

        def __init__(self, daemon: Any, host: str, port: int) -> None:
            pass

        def start(self) -> None:
            order.append("server.start")

        def stop(self) -> None:
            order.append("server.stop")

    def fake_signal(sig: int, handler: Any) -> None:
        handlers[sig] = handler

    monkeypatch.setattr(cli, "read_config", lambda path: object())
    monkeypatch.setattr(cli, "Daemon", FakeDaemon)
    monkeypatch.setattr(cli, "Server", FakeServer)
    monkeypatch.setattr(signal, "signal", fake_signal)

    thread = threading.Thread(
        target=cli.main, args=(["--cron-dir", cron_dir, "--port", "0"],), daemon=True
    )
    thread.start()

    deadline = time.time() + 5
    while signal.SIGTERM not in handlers and time.time() < deadline:
        time.sleep(0.01)
    assert signal.SIGTERM in handlers, "daemon never reached the signal loop"

    handler = handlers[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert order == [
        "refresh",
        "recover",
        "start",
        "server.start",
        "server.stop",
        "stop",
    ]
