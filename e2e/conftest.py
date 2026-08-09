"""Fixtures for the real-Docker end-to-end suite (see REQUIREMENTS.md)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

IMAGE = "busybox:latest"
PREFIX = "supercron-"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return _docker("info").returncode == 0


def _stray_containers() -> set[str]:
    p = _docker("ps", "-a", "--format", "{{.Names}}")
    if p.returncode != 0:
        return set()
    return {name for name in p.stdout.split() if name.startswith(PREFIX)}


@pytest.fixture()
def docker_env():
    """Skip without Docker; fail on leftover containers; clean up afterwards."""
    if not _docker_available():
        pytest.skip("Docker daemon is not available")
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pull = _docker("pull", IMAGE)
        assert pull.returncode == 0, f"docker pull {IMAGE} failed: {pull.stderr}"
    strays = _stray_containers()
    assert not strays, (
        "stray containers from a previous run: "
        + ", ".join(sorted(strays))
        + " (remove them first)"
    )
    yield
    for name in _stray_containers():
        _docker("rm", "-f", name)
    assert not _stray_containers(), "failed to clean up supercron containers"
