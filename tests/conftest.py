"""Shared pytest fixtures."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def silent_logger() -> logging.Logger:
    """Logger that swallows output during tests."""
    logger = logging.getLogger("test")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


@pytest.fixture
def tmp_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def base_config() -> dict:
    """Minimal config matching production defaults."""
    return {
        "timers": {
            "suspend_seconds": 1200,
            "cleanup_seconds": 3600,
            "disconnect_verify_seconds": 8,
            "possible_disconnect_max_seconds": 60,
        },
        "graceful_kill_wait_seconds": 1,
        "cleanup_processes": ["fakegame", "discord"],
        "suspend_games": ["fakegame"],
        "instant_kill_games": ["anticheat"],
        "protected_processes": ["sunshine", "explorer"],
        "stream_processes": ["sunshine", "moonlight"],
        "watchdog": {"interval_seconds": 5, "missing_stream_seconds": 20},
        "connection_check": {
            "enabled": False,
            "ports": [47998, 48010],
            "check_interval_seconds": 600,
            "kill_after_seconds": 3600,
        },
        "resource_monitor": {"enabled": False},
    }


@pytest.fixture
def fake_process_factory():
    """Build a MagicMock psutil.Process-like object."""

    def _make(pid: int, name: str, status: str = "running", alive: bool = True):
        proc = MagicMock()
        proc.pid = pid
        proc.info = {"pid": pid, "name": name, "status": status}
        proc.is_running.return_value = alive
        return proc

    return _make


@pytest.fixture
def fake_conn_factory():
    """Build a MagicMock psutil._common.sconn-like object."""

    class _Addr:
        def __init__(self, ip: str, port: int) -> None:
            self.ip = ip
            self.port = port

    def _make(
        pid: int | None,
        status: str,
        laddr_port: int | None,
        raddr_ip: str | None = "10.0.0.1",
        raddr_port: int = 12345,
    ):
        conn = MagicMock()
        conn.pid = pid
        conn.status = status
        conn.laddr = _Addr("0.0.0.0", laddr_port) if laddr_port is not None else None
        conn.raddr = _Addr(raddr_ip, raddr_port) if raddr_ip else None
        return conn

    return _make
