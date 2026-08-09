"""Persistent-container Docker runner.

Each task owns one long-lived container that is created once (``docker run
-d``) and reused across executions via the ``docker start`` / ``docker stop``
analog. The container runs ``start.sh`` and exits on its own; the exit code is
the source of truth for success/failure.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RootConfig
from .tasks import Task, utcnow


class DockerError(Exception):
    """Raised when a docker command fails."""


@dataclass
class ExecutionResult:
    exit_code: int | None
    timed_out: bool = False
    duration: float = 0.0

    @property
    def success(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class DockerRunner:
    """Creates and drives the persistent container for a task."""

    def __init__(self, config: RootConfig, poll_interval: float = 0.2):
        self.config = config
        self.poll_interval = poll_interval

    # ------------------------------------------------------------ docker

    @staticmethod
    def _cmd(*args: str) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(["docker", *args], capture_output=True, text=True)

    @classmethod
    def available(cls) -> bool:
        return cls._cmd("ps").returncode == 0

    def _ensure_cmd(self, *args: str) -> str:
        p = self._cmd(*args)
        if p.returncode != 0:
            raise DockerError((p.stderr or p.stdout).strip())
        out = p.stdout or ""
        return out.strip()

    # ------------------------------------------------------ container mgmt

    def container_exists(self, task: Task) -> bool:
        return self._cmd("inspect", task.container_name).returncode == 0

    def ensure_container(self, task: Task) -> bool:
        """Create the persistent container if it does not exist.

        Returns True if the container was created, False if it already
        existed. Idempotent.
        """
        if self.container_exists(task):
            return False
        self._ensure_cmd(
            "run",
            "-d",
            "--name",
            task.container_name,
            "-v",
            f"{task.task_dir}:{self.config.mount_path}",
            "-w",
            self.config.mount_path,
            self.config.image,
            f"{self.config.mount_path}/start.sh",
        )
        return True

    def destroy(self, task: Task) -> None:
        """Force-remove the persistent container (for a reset/recreate)."""
        self.remove_container(task.container_name)

    def remove_container(self, name: str) -> None:
        """Force-remove the container with the given name (no-op if absent)."""

        self._cmd("rm", "-f", name)

    def list_containers(self) -> set[str]:
        """Return the names of all ``supercron-*`` containers (if any)."""
        p = self._cmd("ps", "-a", "--format", "{{.Names}}")
        if p.returncode != 0:
            raise DockerError((p.stderr or p.stdout).strip())
        return {name for name in p.stdout.split() if name.startswith("supercron-")}

    def _state(self, task: Task) -> tuple[str, int | None]:
        """Return (status, exit_code) of the container; 'missing' if absent."""
        p = self._cmd(
            "inspect",
            "-f",
            "{{.State.Status}}\t{{.State.ExitCode}}",
            task.container_name,
        )
        if p.returncode != 0:
            return "missing", None
        status, code = p.stdout.strip().split("\t")
        return status, int(code)

    def stop(self, task: Task) -> None:
        """Stop a running container, giving it ``kill_grace`` seconds."""

        self._cmd("stop", "-t", str(self.config.kill_grace), task.container_name)

    def is_running(self, task: Task) -> bool:
        return self._state(task)[0] == "running"

    # ------------------------------------------------------------ execution

    def run_execution(
        self,
        task: Task,
        log_path: Path,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Start the persistent container and wait for it to exit.

        Any still-running container from a previous execution is killed first
        (overlap policy). The run's merged output is captured into
        ``log_path`` once the container has exited (see :meth:`_capture_logs`).
        Returns an ExecutionResult; on timeout the container is stopped and a
        timed_out result is returned.
        """
        started = time.monotonic()
        started_at_iso = utcnow().isoformat(timespec="microseconds")

        state, _ = self._state(task)
        if state == "running":
            self.stop(task)
        elif state == "missing":
            self.ensure_container(task)

        self._ensure_cmd("start", task.container_name)

        timed_out = False
        deadline = timeout + started if timeout else None
        try:
            while True:
                state, _ = self._state(task)
                if state != "running":
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    self.stop(task)
                    timed_out = True
                    break
                time.sleep(self.poll_interval)
        finally:
            self._capture_logs(task, log_path, started_at_iso)

        _, exit_code = self._state(task)
        return ExecutionResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration=time.monotonic() - started,
        )

    def _capture_logs(self, task: Task, log_path: Path, since: str) -> None:
        """Write this run's merged stdout+stderr into ``log_path``.

        Runs ``docker logs --since`` (non-following) after the container has
        exited, so the capture is deterministic and cannot race a live log
        stream. The persistent container accumulates logs across runs, so
        ``--since`` scopes the capture to this run.
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)
        p = self._cmd("logs", "--since", since, task.container_name)
        with log_path.open("w") as log_file:
            log_file.write(p.stdout)
            log_file.write(p.stderr)
