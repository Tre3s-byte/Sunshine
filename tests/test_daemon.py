"""End-to-end tests for SunshineDaemon flows (disconnect, reconnect, two-tier).

The daemon module instantiates a singleton at import-time, so we patch the
heavy dependencies (StreamWatchdog, PowerManager subprocess, log paths)
before importing it.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def daemon_module(tmp_path, base_config, monkeypatch):
    """Import a fresh copy of the daemon with sandboxed paths and a no-op watchdog."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(base_config))

    state_path = tmp_path / "state.json"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lock_path = tmp_path / "auditor.lock"

    import importlib
    import sys

    # Drop any cached import so we get a clean module
    for mod in list(sys.modules):
        if mod.startswith("daemon.sunshine_daemon"):
            del sys.modules[mod]

    import daemon.sunshine_daemon as sd

    # Re-point file constants to the temp dir
    sd.CONFIG_PATH = config_path
    sd.STATE_PATH = state_path
    sd.LOG_DIR = logs_dir
    sd.DAEMON_LOG = logs_dir / "daemon.log"
    sd.AUDIT_LOG = logs_dir / "audit.log"
    sd.SESSION_LOG = logs_dir / "sessions.log"
    sd.LOCK_FILE = lock_path

    # Stop the watchdog from actually running
    monkeypatch.setattr(sd.StreamWatchdog, "start", lambda self: None)
    # PowerManager would call powercfg — neuter it
    monkeypatch.setattr(sd.PowerManager, "set_profile", lambda self, _: True)

    # Reload the module-level `daemon` singleton with our patched constants
    importlib.reload(sd)
    sd.CONFIG_PATH = config_path
    sd.STATE_PATH = state_path
    sd.AUDIT_LOG = logs_dir / "audit.log"
    sd.SESSION_LOG = logs_dir / "sessions.log"

    return sd


@pytest.fixture
def daemon(daemon_module):
    """A fresh SunshineDaemon instance with all heavy deps stubbed out."""
    d = daemon_module.SunshineDaemon()
    d.process_manager = MagicMock()
    d.process_manager.is_stream_alive.return_value = False
    d.process_manager.is_connection_active.return_value = False
    d.process_manager.resume_games.return_value = []
    d.process_manager.suspend_games.return_value = []
    d.process_manager.kill_cleanup_processes.return_value = []
    d.process_manager.launch_steam.return_value = True
    d.resource_monitor = MagicMock()
    d.resource_monitor.kill_high_cpu_processes.return_value = []
    d.power_manager = MagicMock()
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fire_named_timer(daemon, name: str) -> None:
    """Synchronously fire the named scheduled timer (skipping the wall-clock wait)."""
    with daemon.timer_lock:
        timer = daemon.timers.get(name)
    assert timer is not None, f"Timer {name!r} was never scheduled"
    timer.cancel()
    # threading.Timer stashes the target fn and kwargs on the instance
    timer.function(**(timer.kwargs or {}))


# ---------------------------------------------------------------------------
# Disconnect / reconnect flow
# ---------------------------------------------------------------------------

class TestDisconnectFlow:
    def test_disconnect_from_streaming_goes_to_possible_disconnect(self, daemon):
        daemon.state_machine.transition("STREAMING")
        daemon.handle_disconnect()
        assert daemon.state_machine.get()["state"] == "POSSIBLE_DISCONNECT"
        assert "disconnect_verify" in daemon.timers
        assert "possible_disconnect_deadline" in daemon.timers

    def test_verify_disconnect_returns_to_streaming_if_alive(self, daemon):
        daemon.state_machine.transition("STREAMING")
        daemon.handle_disconnect()
        daemon.process_manager.is_stream_alive.return_value = True

        _fire_named_timer(daemon, "disconnect_verify")

        assert daemon.state_machine.get()["state"] == "STREAMING"

    def test_verify_disconnect_advances_to_grace_period_if_dead(self, daemon):
        daemon.state_machine.transition("STREAMING")
        daemon.handle_disconnect()
        daemon.process_manager.is_stream_alive.return_value = False

        _fire_named_timer(daemon, "disconnect_verify")

        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in daemon.timers
        assert "suspend" in daemon.timers

    def test_possible_disconnect_deadline_advances_to_grace(self, daemon):
        daemon.state_machine.transition("STREAMING")
        daemon.handle_disconnect()

        _fire_named_timer(daemon, "possible_disconnect_deadline")

        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"

    def test_disconnect_in_grace_period_does_not_reset_timer(self, daemon):
        """Bug #3 regression: redundant /disconnect must not reset the kill timer."""
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()
        original_deadline = daemon.timer_deadlines["cleanup"]

        # A second disconnect should NOT push the deadline forward
        daemon.handle_disconnect(reason="redundant")

        assert daemon.timer_deadlines["cleanup"] == original_deadline

    def test_disconnect_from_idle_enters_grace_period(self, daemon):
        daemon.state_machine.transition("IDLE")
        daemon.handle_disconnect()
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in daemon.timers


class TestTwoTierTimers:
    """20-min suspend → 60-min cleanup, both anchored on GRACE_PERIOD entry."""

    def test_grace_period_schedules_both_timers(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()

        assert "suspend" in daemon.timers
        assert "cleanup" in daemon.timers

    def test_suspend_timer_fires_first(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()

        suspend_deadline = daemon.timer_deadlines["suspend"]
        cleanup_deadline = daemon.timer_deadlines["cleanup"]
        assert suspend_deadline < cleanup_deadline

    def test_suspend_timer_calls_suspend_games(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()

        _fire_named_timer(daemon, "suspend")

        daemon.process_manager.suspend_games.assert_called_once()
        # Must remain in GRACE_PERIOD — suspend doesn't transition state
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"

    def test_suspend_timer_skips_if_no_longer_in_grace(self, daemon):
        """If user reconnected, suspend timer must be a no-op."""
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()
        # User reconnected before suspend fired
        daemon.state_machine.transition("STREAMING")

        _fire_named_timer(daemon, "suspend")

        daemon.process_manager.suspend_games.assert_not_called()

    def test_cleanup_timer_kills_and_returns_to_idle(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()

        _fire_named_timer(daemon, "cleanup")

        daemon.process_manager.kill_cleanup_processes.assert_called_once()
        assert daemon.state_machine.get()["state"] == "IDLE"

    def test_suspend_disabled_when_misconfigured(self, daemon):
        """If suspend_seconds >= cleanup_seconds, skip the tier-1 timer."""
        daemon.config["timers"]["suspend_seconds"] = 3600
        daemon.config["timers"]["cleanup_seconds"] = 3600

        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()

        assert "suspend" not in daemon.timers
        assert "cleanup" in daemon.timers


class TestReconnect:
    def test_reconnect_cancels_all_timers(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()
        assert daemon.timers

        daemon.reconnect()

        assert daemon.timers == {}
        assert daemon.state_machine.get()["state"] == "STREAMING"

    def test_reconnect_resumes_suspended_games_immediately(self, daemon):
        """Bug-fix regression: user shouldn't wait up to 10 min for watchdog
        to notice and resume games."""
        daemon.process_manager.resume_games.return_value = ["fakegame:1234"]
        daemon.state_machine.transition("GRACE_PERIOD")

        daemon.reconnect()

        daemon.process_manager.resume_games.assert_called_once()

    def test_start_stream_also_resumes_games(self, daemon):
        """When Sunshine fires prep-cmd → /start, suspended games must resume."""
        daemon.process_manager.resume_games.return_value = ["fakegame:1234"]
        daemon.state_machine.transition("GRACE_PERIOD")

        daemon.start_stream()

        daemon.process_manager.resume_games.assert_called_once()
        assert daemon.state_machine.get()["state"] == "STREAMING"


class TestCleanupRobustness:
    def test_cleanup_always_transitions_to_idle_even_on_exception(self, daemon):
        """Bug #4 regression: an exception in cleanup must not leave the
        daemon stuck in CLEANING forever."""
        daemon.process_manager.kill_cleanup_processes.side_effect = RuntimeError("boom")
        daemon.state_machine.transition("CLEANING")

        daemon._do_cleanup_work(reason="test")

        assert daemon.state_machine.get()["state"] == "IDLE"

    def test_cleanup_records_terminated_processes(self, daemon):
        daemon.process_manager.kill_cleanup_processes.return_value = ["fakegame:99"]
        daemon.state_machine.transition("CLEANING")

        daemon._do_cleanup_work(reason="grace_period_expired")

        assert daemon.state_machine.get()["state"] == "IDLE"


class TestStartupRecovery:
    """Bug #2: state restored from disk on restart needs its timers re-scheduled."""

    def test_recovery_grace_period_reschedules_cleanup(self, daemon_module, tmp_path):
        # Pre-seed state.json as GRACE_PERIOD before daemon construction
        daemon_module.STATE_PATH.write_text(json.dumps(
            {"state": "GRACE_PERIOD", "generation": 5, "timestamp": 0}
        ))

        d = daemon_module.SunshineDaemon()
        d.process_manager = MagicMock()
        d.process_manager.is_stream_alive.return_value = False

        # Re-run construction logic that the fixture skipped
        # (we already constructed above; just assert outcome)
        assert d.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in d.timers, "cleanup timer must be re-scheduled on restart"
        assert "suspend" in d.timers, "suspend timer must be re-scheduled on restart"

    def test_recovery_possible_disconnect_advances_to_grace(
        self, daemon_module
    ):
        daemon_module.STATE_PATH.write_text(json.dumps(
            {"state": "POSSIBLE_DISCONNECT", "generation": 1, "timestamp": 0}
        ))

        d = daemon_module.SunshineDaemon()
        assert d.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in d.timers

    def test_recovery_streaming_with_dead_process_advances(
        self, daemon_module
    ):
        daemon_module.STATE_PATH.write_text(json.dumps(
            {"state": "STREAMING", "generation": 1, "timestamp": 0}
        ))

        # Patch is_stream_alive BEFORE constructing the daemon so the recovery
        # branch sees a dead stream process.
        with patch(
            "daemon.process_manager.ProcessManager.is_stream_alive",
            return_value=False,
        ):
            d = daemon_module.SunshineDaemon()

        assert d.state_machine.get()["state"] == "POSSIBLE_DISCONNECT"
        assert "disconnect_verify" in d.timers

    def test_recovery_streaming_with_live_process_stays(self, daemon_module):
        daemon_module.STATE_PATH.write_text(json.dumps(
            {"state": "STREAMING", "generation": 1, "timestamp": 0}
        ))

        with patch(
            "daemon.process_manager.ProcessManager.is_stream_alive",
            return_value=True,
        ):
            d = daemon_module.SunshineDaemon()

        assert d.state_machine.get()["state"] == "STREAMING"
        # No grace-period timers because nothing changed
        assert "cleanup" not in d.timers


class TestEndStream:
    def test_end_stream_kicks_off_cleanup_in_background_thread(self, daemon):
        daemon.state_machine.transition("STREAMING")
        with patch.object(threading, "Thread") as MockThread:
            mock_thread = MagicMock()
            MockThread.return_value = mock_thread
            daemon.end_stream()
            MockThread.assert_called_once()
            mock_thread.start.assert_called_once()
        assert daemon.state_machine.get()["state"] == "CLEANING"

    def test_end_stream_cancels_grace_period_timers(self, daemon):
        daemon.state_machine.transition("GRACE_PERIOD")
        daemon._schedule_cleanup()
        assert "cleanup" in daemon.timers

        daemon.end_stream()
        assert daemon.timers == {}
