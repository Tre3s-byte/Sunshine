"""Integration tests simulating the full user scenario.

The user streams from a phone (Moonlight). Several scenarios are exercised:

  S1: Cierre limpio del cliente → detach-cmd → /disconnect → grace period.
      A 20m suspendemos; a 60m matamos todo.

  S2: Reconexión dentro de los 20m → juegos siguen corriendo, nada se mata.

  S3: Reconexión entre 20m y 60m → resume_games() corre inmediato, no
      hay que esperar al watchdog.

  S4: No hay reconexión → cleanup_processes mueren a los 60m.

  S5: Reinicio del daemon estando en GRACE_PERIOD → timers se reagendan.

Los tiempos se aceleran configurando suspend_seconds y cleanup_seconds a
valores pequeños y disparando los timers manualmente.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def daemon_module(tmp_path, base_config, monkeypatch):
    """Same setup as in test_daemon.py — copied to keep tests independent."""
    config_path = tmp_path / "config.json"
    # Accelerated timings: 50ms suspend, 100ms cleanup, 10ms verify
    base_config["timers"] = {
        "suspend_seconds": 1,
        "cleanup_seconds": 2,
        "disconnect_verify_seconds": 1,
        "possible_disconnect_max_seconds": 1,
    }
    config_path.write_text(json.dumps(base_config))

    state_path = tmp_path / "state.json"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lock_path = tmp_path / "auditor.lock"

    import importlib
    import sys

    for mod in list(sys.modules):
        if mod.startswith("daemon.sunshine_daemon"):
            del sys.modules[mod]

    import daemon.sunshine_daemon as sd

    sd.CONFIG_PATH = config_path
    sd.STATE_PATH = state_path
    sd.AUDIT_LOG = logs_dir / "audit.log"
    sd.SESSION_LOG = logs_dir / "sessions.log"
    sd.LOCK_FILE = lock_path

    monkeypatch.setattr(sd.StreamWatchdog, "start", lambda self: None)
    monkeypatch.setattr(sd.PowerManager, "set_profile", lambda self, _: True)

    importlib.reload(sd)
    sd.CONFIG_PATH = config_path
    sd.STATE_PATH = state_path
    sd.AUDIT_LOG = logs_dir / "audit.log"
    sd.SESSION_LOG = logs_dir / "sessions.log"
    return sd


@pytest.fixture
def daemon(daemon_module):
    d = daemon_module.SunshineDaemon()
    d.process_manager = MagicMock()
    d.process_manager.is_stream_alive.return_value = False
    d.process_manager.is_connection_active.return_value = False
    d.process_manager.resume_games.return_value = []
    d.process_manager.suspend_games.return_value = ["fakegame:1234"]
    d.process_manager.kill_cleanup_processes.return_value = ["fakegame:1234"]
    d.process_manager.launch_steam.return_value = True
    d.resource_monitor = MagicMock()
    d.resource_monitor.kill_high_cpu_processes.return_value = []
    d.power_manager = MagicMock()
    return d


def _fire(daemon, name: str) -> None:
    timer = daemon.timers.get(name)
    assert timer is not None, f"Timer {name!r} not scheduled"
    timer.cancel()
    timer.function(**(timer.kwargs or {}))


# ---------------------------------------------------------------------------
# Full user scenarios
# ---------------------------------------------------------------------------

class TestScenarioS1_CleanDisconnect:
    """Caso típico: phone Moonlight cierra → detach-cmd → /disconnect.

    Sunshine's detach-cmd is authoritative; we go directly to GRACE_PERIOD.
    There is no verification step because is_stream_alive() always returns
    True (sunshine.exe runs as a service).
    """

    def test_full_path_to_kill(self, daemon):
        # 1. Estábamos streameando
        daemon.start_stream()
        assert daemon.state_machine.get()["state"] == "STREAMING"

        # 2. detach-cmd dispara /disconnect → directo a GRACE_PERIOD
        daemon.handle_disconnect(reason="detach_cmd")
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "suspend" in daemon.timers
        assert "cleanup" in daemon.timers

        # 3. A los 20 min: tier-1 suspende
        _fire(daemon, "suspend")
        daemon.process_manager.suspend_games.assert_called_once()
        # GRACE_PERIOD se mantiene (no transiciona)
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"

        # 4. A los 60 min: tier-2 mata todo
        _fire(daemon, "cleanup")
        daemon.process_manager.kill_cleanup_processes.assert_called_once()
        assert daemon.state_machine.get()["state"] == "IDLE"


class TestScenarioS2_ReconnectBeforeSuspend:
    """Reconexión antes de los 20 min → todo sigue corriendo."""

    def test_reconnect_cancels_both_timers(self, daemon):
        daemon.start_stream()
        daemon.handle_disconnect()
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"

        # User vuelve antes de los 20m
        daemon.start_stream()
        assert daemon.state_machine.get()["state"] == "STREAMING"
        assert daemon.timers == {}

        # No se suspendió, no se mató
        daemon.process_manager.suspend_games.assert_not_called()
        daemon.process_manager.kill_cleanup_processes.assert_not_called()


class TestScenarioS3_ReconnectAfterSuspend:
    """Reconexión entre 20m y 60m → resume inmediato."""

    def test_reconnect_resumes_immediately(self, daemon):
        daemon.start_stream()
        daemon.handle_disconnect()

        # 20m: suspend
        _fire(daemon, "suspend")
        daemon.process_manager.suspend_games.assert_called_once()
        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in daemon.timers  # cleanup timer aún corriendo

        # Reconexión vía /start (prep-cmd de Sunshine)
        daemon.process_manager.resume_games.return_value = ["fakegame:1234"]
        daemon.start_stream()

        # Estado vuelve a STREAMING, resume_games corrió inmediato
        assert daemon.state_machine.get()["state"] == "STREAMING"
        daemon.process_manager.resume_games.assert_called()
        # Timer de cleanup cancelado
        assert "cleanup" not in daemon.timers


class TestScenarioS4_NoReconnect:
    """Sin reconexión → cleanup mata todo a los 60m."""

    def test_kills_after_full_grace_period(self, daemon):
        daemon.start_stream()
        daemon.handle_disconnect()
        _fire(daemon, "suspend")
        _fire(daemon, "cleanup")

        assert daemon.state_machine.get()["state"] == "IDLE"
        daemon.process_manager.kill_cleanup_processes.assert_called_once()
        daemon.power_manager.set_profile.assert_called()  # balanced


class TestScenarioS5_DaemonRestart:
    """Reinicio del daemon en GRACE_PERIOD → timers se restauran."""

    def test_grace_period_timers_restored_on_restart(self, daemon_module):
        # Simular state.json indicando GRACE_PERIOD desde una sesión anterior
        daemon_module.STATE_PATH.write_text(json.dumps(
            {"state": "GRACE_PERIOD", "generation": 3, "timestamp": 0}
        ))

        d = daemon_module.SunshineDaemon()

        assert d.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "suspend" in d.timers
        assert "cleanup" in d.timers


class TestScenarioS6_RedundantDisconnects:
    """Eventos /disconnect repetidos no deben reiniciar el reloj."""

    def test_multiple_disconnects_preserve_original_deadline(self, daemon):
        daemon.start_stream()
        daemon.handle_disconnect()
        original_deadline = daemon.timer_deadlines["cleanup"]

        # 5 desconexiones repetidas (ej: script que reintenta)
        for _ in range(5):
            daemon.handle_disconnect()

        assert daemon.timer_deadlines["cleanup"] == original_deadline


class TestScenarioS7_SunshineDetachIsAuthoritative:
    """Regression: even when sunshine.exe is reported alive, /disconnect must
    transition to GRACE_PERIOD. The old verification step would trap the
    daemon in STREAMING forever because sunshine.exe runs as a service and
    is_stream_alive() always returned True.
    """

    def test_disconnect_advances_even_if_stream_process_alive(self, daemon):
        daemon.start_stream()
        # Sunshine.exe sigue vivo (siempre lo está) — antes esto se trataba
        # como falsa alarma. Ahora confiamos en detach-cmd.
        daemon.process_manager.is_stream_alive.return_value = True

        daemon.handle_disconnect(reason="detach_cmd")

        assert daemon.state_machine.get()["state"] == "GRACE_PERIOD"
        assert "cleanup" in daemon.timers
        assert "suspend" in daemon.timers


class TestScenarioS8_CleanupSurvivesException:
    """Si el cleanup falla, el daemon no debe quedar atascado."""

    def test_cleanup_exception_still_resets_to_idle(self, daemon):
        daemon.start_stream()
        daemon.handle_disconnect()

        daemon.process_manager.kill_cleanup_processes.side_effect = OSError("hardware busy")
        _fire(daemon, "cleanup")

        # Aun así llega a IDLE
        assert daemon.state_machine.get()["state"] == "IDLE"
