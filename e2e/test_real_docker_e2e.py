"""Real-Docker end-to-end test: supercron CLI + web API + real containers.

Drives the actual ``python -m supercron`` daemon as a subprocess with real
Docker containers and real wall-clock scheduling, exactly the way the web UI
does. Scope, timeline and assertions are specified in ``./REQUIREMENTS.md``.

Run with ``make e2e`` (~10 minutes, plus shutdown).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from supercron.cron import CronSchedule
from supercron.tasks import utcnow

IMAGE = "busybox:latest"
PREFIX = "supercron-"
CRON_OFFSETS = (2, 6)  # minutes after the base time when task A fires
RUN_WINDOW = int(os.environ.get("E2E_WINDOW", "600"))  # seconds of live soak
FIRE_TOLERANCE = 90  # seconds of slack around a predicted fire instant
TASK_TIMEOUT = 20  # per-task timeout for timeouttask
KILL_GRACE = 5

EXPECTED_STATUS = {
    "every3": "success",
    "manual2": "success",
    "alwaysfail": "failure",
    "timeouttask": "success",
}

TASK_NAMES = {"every3", "manual2", "alwaysfail", "timeouttask"}


# ------------------------------------------------------------------ helpers


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def container_state(name: str) -> str:
    p = docker("inspect", "-f", "{{.State.Status}}", name)
    if p.returncode != 0:
        return "missing"
    return p.stdout.strip()


def stray_containers() -> set[str]:
    p = docker("ps", "-a", "--format", "{{.Names}}")
    if p.returncode != 0:
        return set()
    return {name for name in p.stdout.split() if name.startswith(PREFIX)}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            content_type = resp.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise AssertionError(f"{method} {url}: HTTP {exc.code}: {detail}") from exc
    if content_type == "application/json":
        return json.loads(body)
    return body.decode()


def wait_until(
    predicate: Callable[[], bool],
    timeout: float,
    interval: float = 0.25,
    msg: str = "condition not met",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s: {msg}")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def record_status(base_url: str, name: str, eid: int) -> str | None:
    for rec in http("GET", base_url + "/api/task/" + name):
        if rec["id"] == eid:
            return str(rec["status"])
    return None


def fetch(base_url: str, name: str, eid: int) -> dict[str, Any]:
    for rec in http("GET", base_url + "/api/task/" + name):
        if rec["id"] == eid:
            return dict(rec)
    raise AssertionError(f"record {name}#{eid} not found")


def leaked_docker_procs() -> bool:
    if shutil.which("pgrep") is None:
        raise RuntimeError(
            "pgrep is required to verify that no docker subprocess leaked, "
            "but it is not installed"
        )
    pattern = rf"docker (logs|start|stop) {PREFIX}"
    p = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return p.returncode == 0


class DaemonProc:
    """A supercron CLI subprocess with stderr capture and a ready signal."""

    def __init__(
        self, proc: subprocess.Popen[str], log_path: Path | None = None
    ) -> None:
        self.proc = proc
        self.ready = threading.Event()
        self.stderr_lines: list[str] = []
        self._log_fh = log_path.open("w") if log_path else None
        threading.Thread(target=self._read, daemon=True).start()

    @classmethod
    def spawn(
        cls, cron_dir: Path, port: int, log_path: Path | None = None
    ) -> DaemonProc:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "supercron",
                "--cron-dir",
                str(cron_dir),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cls(proc, log_path)

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))
            if self._log_fh:
                self._log_fh.write(line)
                self._log_fh.flush()
            if "supercron ready" in line:
                self.ready.set()

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready.is_set():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise AssertionError(f"daemon did not become ready; stderr:\n{self.log_tail()}")

    def log_tail(self, n: int = 40) -> str:
        return "\n".join(self.stderr_lines[-n:])

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        return self.proc.returncode if self.proc.returncode is not None else -1


# ------------------------------------------------------------------- setup


def build_cron_root(root: Path) -> str:
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        f'image = "{IMAGE}"\n'
        'mount_path = "/work"\n'
        "timeout = 300\n"
        f"kill_grace = {KILL_GRACE}\n"
    )

    now = utcnow()
    base = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes = sorted((base.minute + k) % 60 for k in CRON_OFFSETS)
    expression = f"{minutes[0]},{minutes[1]} * * * *"

    def add_task(name: str, script: str, cron_toml: str = "") -> None:
        task_dir = root / "tasks" / name
        task_dir.mkdir(parents=True)
        (task_dir / "start.sh").write_text(script)
        (task_dir / "start.sh").chmod(0o755)
        if cron_toml:
            (task_dir / "cron.toml").write_text(cron_toml)

    add_task(
        "every3",
        "#!/bin/sh\necho every3-start\nsleep 60\n",
        f'title = "every3"\nschedule = "{expression}"\n',
    )
    add_task("manual2", "#!/bin/sh\necho manual2-start\nsleep 30\n")
    add_task("alwaysfail", "#!/bin/sh\necho failing-start\nexit 1\n")
    add_task(
        "timeouttask",
        "#!/bin/sh\necho timeout-start\nsleep 3600\n",
        f'title = "timeouttask"\ntimeout = {TASK_TIMEOUT}\n',
    )
    return expression


# -------------------------------------------------------------------- test


def test_real_docker_e2e(tmp_path, docker_env):
    cron_dir = tmp_path / "cron"
    expression = build_cron_root(cron_dir)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    daemon = DaemonProc.spawn(cron_dir, port, log_path=tmp_path / "daemon.log")

    started = time.monotonic()

    def since() -> float:
        return time.monotonic() - started

    def step(label: str) -> None:
        print(f"[e2e +{since():5.0f}s] {label}", flush=True)

    try:
        daemon.wait_ready(45)
        step(f"daemon ready; every3 cron expression: {expression!r}")

        def all_tasks_present() -> bool:
            names = {t["name"] for t in http("GET", base_url + "/api/status")}
            return names == TASK_NAMES

        wait_until(
            all_tasks_present,
            30,
            msg="tasks not all visible in /api/status",
        )
        status = {t["name"]: t for t in http("GET", base_url + "/api/status")}
        fire0 = parse_dt(status["every3"]["next_run"])
        fires = [fire0, CronSchedule.parse(expression).next_after(fire0)]
        step(
            f"4 tasks visible; every3 expected fires at "
            f"{fires[0]:%H:%M:%S} and {fires[1]:%H:%M:%S} UTC"
        )

        step("manual run B #1 (mock UI POST /task/manual2/run)")
        http("POST", base_url + "/task/manual2/run")
        wait_until(
            lambda: record_status(base_url, "manual2", 1) == "success",
            90,
            msg="manual2 run #1 did not finish successfully",
        )

        step(f"waiting for every3 cron fire #1 (~{fires[0]:%H:%M:%S} UTC)")
        wait_until(
            lambda: record_status(base_url, "every3", 1) not in (None, "running"),
            240,
            msg="every3 run #1 never finalized",
        )
        rec1 = fetch(base_url, "every3", 1)
        assert rec1["trigger"] == "cron"
        assert (
            abs((parse_dt(rec1["started_at"]) - fires[0]).total_seconds())
            <= FIRE_TOLERANCE
        )
        log1 = http("GET", base_url + f"/task/every3/log/{rec1['id']}")
        assert "every3-start" in log1
        step(f"every3 run #1 {rec1['status']} (return_code={rec1.get('return_code')})")

        step("manual run of alwaysfail (expected failure)")
        http("POST", base_url + "/task/alwaysfail/run")
        wait_until(
            lambda: record_status(base_url, "alwaysfail", 1) == "failure",
            60,
            msg="alwaysfail run #1 did not fail",
        )
        rec_c = fetch(base_url, "alwaysfail", 1)
        assert rec_c.get("return_code") == 1
        step(f"alwaysfail run #1 failure (return_code={rec_c.get('return_code')})")

        step("manual run of timeouttask (expected timeout kill)")
        http("POST", base_url + "/task/timeouttask/run")
        wait_until(
            lambda: record_status(base_url, "timeouttask", 1) == "failure",
            90,
            msg="timeouttask run #1 did not finalize as failure",
        )
        rec_d1 = fetch(base_url, "timeouttask", 1)
        rc = rec_d1.get("return_code")
        assert rc is not None and rc != 0
        duration = (
            parse_dt(rec_d1["ended_at"]) - parse_dt(rec_d1["started_at"])
        ).total_seconds()
        assert TASK_TIMEOUT <= duration <= TASK_TIMEOUT + KILL_GRACE + 15
        assert container_state(PREFIX + "timeouttask") == "exited"
        step(
            f"timeouttask run #1 failure (return_code={rc}, "
            f"duration={duration:.1f}s, container exited)"
        )

        step("re-triggering timeouttask after disposal (start.sh now exits 0)")
        start_d = cron_dir / "tasks" / "timeouttask" / "start.sh"
        start_d.write_text("#!/bin/sh\necho timeout-reuse\nexit 0\n")
        start_d.chmod(0o755)
        http("POST", base_url + "/task/timeouttask/run")
        wait_until(
            lambda: record_status(base_url, "timeouttask", 2) == "success",
            90,
            msg="timeouttask run #2 (reuse) did not succeed",
        )
        rec_d2 = fetch(base_url, "timeouttask", 2)
        assert rec_d2.get("return_code") == 0
        assert container_state(PREFIX + "timeouttask") == "exited"
        step("timeouttask run #2 success; killed container is reusable")

        step("manual run B #2")
        http("POST", base_url + "/task/manual2/run")
        wait_until(
            lambda: record_status(base_url, "manual2", 2) == "success",
            90,
            msg="manual2 run #2 did not finish successfully",
        )

        step(f"waiting for every3 cron fire #2 (~{fires[1]:%H:%M:%S} UTC)")
        wait_until(
            lambda: record_status(base_url, "every3", 2) not in (None, "running"),
            240,
            msg="every3 run #2 never finalized",
        )
        rec2 = fetch(base_url, "every3", 2)
        assert rec2["trigger"] == "cron"
        assert (
            abs((parse_dt(rec2["started_at"]) - fires[1]).total_seconds())
            <= FIRE_TOLERANCE
        )
        assert parse_dt(rec1["ended_at"]) < parse_dt(rec2["started_at"])
        step("every3 run #2 done; the two runs never overlapped")

        remaining = started + RUN_WINDOW - time.monotonic()
        if remaining > 0:
            step(f"soaking for {remaining:.0f}s more ({RUN_WINDOW}s total)")
            time.sleep(remaining)

        step("final observation via the web API and disk")
        page = http("GET", base_url + "/")
        for name in TASK_NAMES:
            assert name in page

        reported = {
            t["name"]: t["status"] for t in http("GET", base_url + "/api/status")
        }
        assert reported == EXPECTED_STATUS, f"unexpected statuses: {reported}"

        expected_counts = {
            "every3": 2,
            "manual2": 2,
            "alwaysfail": 1,
            "timeouttask": 2,
        }
        for name, count in expected_counts.items():
            history = http("GET", base_url + "/api/task/" + name)
            assert len(history) == count, (
                f"{name}: expected {count} runs, got {len(history)}"
            )
        assert [r["trigger"] for r in http("GET", base_url + "/api/task/every3")] == [
            "cron",
            "cron",
        ]
        assert [r["status"] for r in http("GET", base_url + "/api/task/every3")] == [
            "success",
            "success",
        ]
        assert [r["trigger"] for r in http("GET", base_url + "/api/task/manual2")] == [
            "manual",
            "manual",
        ]
        assert [r["status"] for r in http("GET", base_url + "/api/task/manual2")] == [
            "success",
            "success",
        ]

        for name in expected_counts:
            results_dir = cron_dir / "results" / name
            disk_ids = {int(p.stem) for p in results_dir.glob("*.toml")}
            api_ids = {r["id"] for r in http("GET", base_url + "/api/task/" + name)}
            assert disk_ids == api_ids, (
                f"{name} disk/API mismatch: {disk_ids} vs {api_ids}"
            )
            for log_path in results_dir.glob("*.log"):
                assert log_path.stat().st_size > 0, f"{name}: empty log {log_path.name}"
        step("UI/API counts, statuses and on-disk records all consistent")

        wait_until(
            lambda: not leaked_docker_procs(),
            10,
            msg="a docker subprocess leaked for a supercron container",
        )

        step("SIGTERM daemon")
        rc = daemon.stop()
        assert rc == 0, (
            f"daemon did not shut down cleanly; stderr:\n{daemon.log_tail()}"
        )
        step("daemon exited cleanly with rc=0")
    finally:
        if daemon.proc.poll() is None:
            daemon.stop()
        for name in stray_containers():
            docker("rm", "-f", name)
