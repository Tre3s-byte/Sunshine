import atexit
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psutil
from flask import Flask, jsonify, request

from daemon.core import StateMachine
from daemon.process_manager import ProcessManager
from daemon.resource_monitor import ResourceMonitor
from daemon.watchdog import StreamWatchdog


BASE_DIR = REPO_ROOT
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
DAEMON_LOG = LOG_DIR / "daemon.log"
AUDIT_LOG = LOG_DIR / "audit.log"
LOCK_FILE = BASE_DIR / "auditor.lock"
HOST = "127.0.0.1"
PORT = 8765


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [DAEMON] %(message)s",
        handlers=[
            logging.FileHandler(DAEMON_LOG, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("sunshine-daemon")


logger = setup_logging()
app = Flask(__name__)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read config.json; using defaults")
        return {}


def process_exists(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid)
    except (psutil.Error, ValueError):
        return False


def acquire_lock() -> None:
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            if process_exists(old_pid):
                raise SystemExit(f"Sunshine daemon already running with pid {old_pid}")
        except ValueError:
            pass

    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


class SunshineDaemon:
    def __init__(self):
        self.config = load_config()
        self.state_machine = StateMachine(STATE_PATH, logger)
        self.process_manager = ProcessManager(self.config, logger)
        self.resource_monitor = ResourceMonitor(self.config, logger)
        self.timers: dict[str, threading.Timer] = {}
        self.timer_lock = threading.RLock()
        self.last_api_call = 0.0
        self.watchdog = StreamWatchdog(self, logger)
        self.watchdog.start()

    def verify_token(self) -> bool:
        expected = self.config.get("api_token")
        if not expected:
            return True
        return request.headers.get("X-API-Token") == expected

    def rate_limit_ok(self) -> bool:
        now = time.time()
        if now - self.last_api_call < float(self.config.get("api_rate_limit_seconds", 0.5)):
            return False
        self.last_api_call = now
        return True

    def audit(self, event: str, **details) -> None:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": int(time.time()),
            "event": event,
            "state": self.state_machine.get()["state"],
            "details": details,
        }
        with AUDIT_LOG.open("a", encoding="utf-8") as audit_file:
            audit_file.write(json.dumps(payload, sort_keys=True) + "\n")
        logger.info("EVENT %s | state=%s", event, payload["state"])

    def cancel_timers(self) -> None:
        with self.timer_lock:
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()

    def start_stream(self) -> dict:
        self.cancel_timers()
        self.state_machine.transition("STARTING")
        self.audit("start")
        self.state_machine.transition("STREAMING")
        return self.state_machine.get()

    def end_stream(self) -> dict:
        self.audit("end")
        return self.cleanup(reason="end")

    def handle_disconnect(self, reason: str = "disconnect") -> dict:
        current_state = self.state_machine.get()["state"]
        self.audit("disconnect", reason=reason)

        if current_state == "STREAMING":
            self.state_machine.transition("POSSIBLE_DISCONNECT")
            self.schedule_verify_disconnect()
        elif current_state == "GRACE_PERIOD":
            self.schedule_cleanup()
        elif current_state in {"IDLE", "STARTING"}:
            self.state_machine.transition("GRACE_PERIOD")
            self.schedule_cleanup()

        return self.state_machine.get()

    def reconnect(self) -> dict:
        self.cancel_timers()
        self.audit("reconnect")
        self.state_machine.transition("STREAMING")
        return self.state_machine.get()

    def schedule_verify_disconnect(self) -> None:
        delay = int(self.config.get("timers", {}).get("disconnect_verify_seconds", 8))
        timer = threading.Timer(delay, self.verify_disconnect)
        timer.daemon = True
        with self.timer_lock:
            self.timers["disconnect_verify"] = timer
        logger.info("Disconnect verification scheduled in %ss", delay)
        timer.start()

    def verify_disconnect(self) -> None:
        if self.state_machine.get()["state"] != "POSSIBLE_DISCONNECT":
            return

        if self.process_manager.is_stream_alive():
            logger.info("False disconnect; stream process is still alive")
            self.state_machine.transition("STREAMING")
            return

        self.state_machine.transition("GRACE_PERIOD")
        self.schedule_cleanup()

    def schedule_cleanup(self) -> None:
        self.cancel_timers()
        delay = int(self.config.get("timers", {}).get("cleanup_seconds", 1800))
        timer = threading.Timer(delay, self.cleanup, kwargs={"reason": "grace_period_expired"})
        timer.daemon = True
        with self.timer_lock:
            self.timers["cleanup"] = timer
        logger.info("Cleanup scheduled in %ss", delay)
        timer.start()

    def cleanup(self, reason: str) -> dict:
        self.cancel_timers()
        self.state_machine.transition("CLEANING")
        terminated_processes = self.process_manager.kill_cleanup_processes()
        high_cpu_processes = self.resource_monitor.kill_high_cpu_processes()
        self.audit(
            "cleanup",
            reason=reason,
            terminated_processes=terminated_processes,
            high_cpu_processes=high_cpu_processes,
        )
        self.state_machine.transition("IDLE")
        return self.state_machine.get()


daemon = SunshineDaemon()


def guarded(handler):
    if not daemon.rate_limit_ok():
        return jsonify({"ok": False, "error": "rate-limited"}), 429
    if not daemon.verify_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = handler()
    return jsonify({"ok": True, **data})


@app.route("/start", methods=["POST"])
def start_route():
    return guarded(daemon.start_stream)


@app.route("/end", methods=["POST"])
def end_route():
    return guarded(daemon.end_stream)


@app.route("/disconnect", methods=["POST"])
def disconnect_route():
    return guarded(daemon.handle_disconnect)


@app.route("/reconnect", methods=["POST"])
def reconnect_route():
    return guarded(daemon.reconnect)


@app.route("/state", methods=["GET"])
def state_route():
    return jsonify(daemon.state_machine.get())


@app.route("/health", methods=["GET"])
def health_route():
    return jsonify({"ok": True, **daemon.state_machine.get()})


def main() -> None:
    acquire_lock()
    atexit.register(release_lock)
    logger.info("Sunshine daemon starting on %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
