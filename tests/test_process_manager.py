"""Tests for ProcessManager — focus on the connection-check and cleanup bugs."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import psutil
import pytest

from daemon.process_manager import ProcessManager


@pytest.fixture
def pm(base_config, silent_logger):
    return ProcessManager(base_config, silent_logger)


class TestIsConnectionActive:
    """The bug: ESTABLISHED TCP from the Sunshine PID on non-streaming ports
    (web UI 47990, internal connections) was falsely reporting an active stream,
    so the disconnect was never detected and processes never died."""

    def test_pid_scoped_excludes_web_ui_port(self, pm, fake_conn_factory):
        """A PID-scoped check must ignore Sunshine's web UI (47990)."""
        conns = [
            fake_conn_factory(pid=42, status="ESTABLISHED", laddr_port=47990),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is False

    def test_pid_scoped_detects_streaming_port(self, pm, fake_conn_factory):
        conns = [
            fake_conn_factory(pid=42, status="ESTABLISHED", laddr_port=47998),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is True

    def test_pid_scoped_ignores_non_established(self, pm, fake_conn_factory):
        conns = [
            fake_conn_factory(pid=42, status="TIME_WAIT", laddr_port=47998),
            fake_conn_factory(pid=42, status="CLOSE_WAIT", laddr_port=47998),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is False

    def test_pid_scoped_ignores_other_pids(self, pm, fake_conn_factory):
        conns = [
            fake_conn_factory(pid=99, status="ESTABLISHED", laddr_port=47998),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is False

    def test_pid_scoped_requires_raddr(self, pm, fake_conn_factory):
        """A listening socket with no remote address must not count as active."""
        conns = [
            fake_conn_factory(
                pid=42, status="ESTABLISHED", laddr_port=47998, raddr_ip=None
            ),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is False

    def test_mixed_connections_returns_true_when_any_streaming(
        self, pm, fake_conn_factory
    ):
        """Web UI + streaming → True (because streaming is active)."""
        conns = [
            fake_conn_factory(pid=42, status="ESTABLISHED", laddr_port=47990),  # web UI
            fake_conn_factory(pid=42, status="ESTABLISHED", laddr_port=48010),  # stream
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids={42}
            ) is True

    def test_fallback_port_based_works(self, pm, fake_conn_factory):
        """When no stream_pids are cached, fall back to port-based scan."""
        conns = [
            fake_conn_factory(pid=99, status="ESTABLISHED", laddr_port=47998),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids=None
            ) is True

    def test_fallback_port_based_filters_correctly(self, pm, fake_conn_factory):
        conns = [
            fake_conn_factory(pid=99, status="ESTABLISHED", laddr_port=47990),
        ]
        with patch("psutil.net_connections", return_value=conns):
            assert pm.is_connection_active(
                ports=[47998, 48010], stream_pids=None
            ) is False

    def test_psutil_error_returns_false_not_raises(self, pm):
        with patch("psutil.net_connections", side_effect=psutil.AccessDenied()):
            assert pm.is_connection_active(stream_pids={42}) is False


class TestKillCleanup:
    def test_no_cleanup_processes_configured(self, silent_logger, fake_process_factory):
        pm = ProcessManager({"cleanup_processes": []}, silent_logger)
        with patch("psutil.process_iter", return_value=[]):
            assert pm.kill_cleanup_processes() == []

    def test_protected_processes_are_skipped(
        self, pm, fake_process_factory
    ):
        sunshine = fake_process_factory(1, "sunshine.exe")  # protected
        game = fake_process_factory(2, "fakegame.exe")  # cleanup target

        with patch("psutil.process_iter", return_value=[sunshine, game]):
            with patch("subprocess.Popen"):
                game.is_running.return_value = True
                pm.kill_cleanup_processes()

        sunshine.kill.assert_not_called()
        # game should have been killed (force-kill phase)
        game.kill.assert_called()

    def test_suspended_processes_are_resumed_before_kill(
        self, pm, fake_process_factory
    ):
        """Suspended cleanup targets must be resumed first so taskkill works."""
        game = fake_process_factory(
            2, "fakegame.exe", status=psutil.STATUS_STOPPED
        )

        with patch("psutil.process_iter", return_value=[game]):
            with patch("subprocess.Popen"):
                game.is_running.return_value = True
                pm.kill_cleanup_processes()

        game.resume.assert_called_once()
        game.kill.assert_called()

    def test_normalize_strips_exe_suffix(self):
        assert ProcessManager.normalize("Chrome.exe") == "chrome"
        assert ProcessManager.normalize("FakeGame.EXE") == "fakegame"
        assert ProcessManager.normalize(None) == ""


class TestSuspendResume:
    def test_suspend_targets_only(self, pm, fake_process_factory):
        target = fake_process_factory(2, "fakegame.exe")
        other = fake_process_factory(3, "notepad.exe")

        with patch("psutil.process_iter", return_value=[target, other]):
            affected = pm.suspend_games()

        target.suspend.assert_called_once()
        other.suspend.assert_not_called()
        assert any("fakegame" in entry for entry in affected)

    def test_instant_kill_targets_killed_not_suspended(self, pm, fake_process_factory):
        anticheat = fake_process_factory(5, "anticheat.exe")

        with patch("psutil.process_iter", return_value=[anticheat]):
            pm.suspend_games()

        anticheat.kill.assert_called_once()
        anticheat.suspend.assert_not_called()

    def test_protected_processes_are_never_touched(self, pm, fake_process_factory):
        sunshine = fake_process_factory(1, "sunshine.exe")
        with patch("psutil.process_iter", return_value=[sunshine]):
            pm.suspend_games()
        sunshine.suspend.assert_not_called()
        sunshine.kill.assert_not_called()

    def test_already_stopped_process_not_re_suspended(self, pm, fake_process_factory):
        target = fake_process_factory(
            2, "fakegame.exe", status=psutil.STATUS_STOPPED
        )
        with patch("psutil.process_iter", return_value=[target]):
            pm.suspend_games()
        target.suspend.assert_not_called()

    def test_resume_only_stopped_targets(self, pm, fake_process_factory):
        stopped = fake_process_factory(
            2, "fakegame.exe", status=psutil.STATUS_STOPPED
        )
        running = fake_process_factory(3, "fakegame.exe", status="running")

        with patch("psutil.process_iter", return_value=[stopped, running]):
            resumed = pm.resume_games()

        stopped.resume.assert_called_once()
        running.resume.assert_not_called()
        assert len(resumed) == 1
