import atexit
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psutil
from flask import Flask, jsonify, request
from waitress import serve

from daemon.core import StateMachine
from daemon.metrics import MetricsTracker
from daemon.power_manager import PowerManager
from daemon.process_manager import ProcessManager
from daemon.resource_monitor import ResourceMonitor
from daemon.session_tracker import SessionTracker
from daemon.watchdog import StreamWatchdog


BASE_DIR = REPO_ROOT
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
DAEMON_LOG = LOG_DIR / "daemon.log"
AUDIT_LOG = LOG_DIR / "audit.log"
SESSION_LOG = LOG_DIR / "sessions.log"
LOCK_FILE = BASE_DIR / "auditor.lock"
HOST = "127.0.0.1"
PORT = 8765


class _JsonFormatter(logging.Formatter):
    """Writes each log record as a single JSON line to daemon.log."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(DAEMON_LOG, encoding="utf-8")
    file_handler.setFormatter(_JsonFormatter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [DAEMON] %(message)s"))

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
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
        self.metrics = MetricsTracker()
        self.state_machine = StateMachine(STATE_PATH, logger)
        self.process_manager = ProcessManager(self.config, logger, metrics=self.metrics)
        self.resource_monitor = ResourceMonitor(self.config, logger)
        self.power_manager = PowerManager(self.config, logger)
        self.session_tracker = SessionTracker(SESSION_LOG, logger)
        self.timers: dict[str, threading.Timer] = {}
        self.timer_deadlines: dict[str, float] = {}
        self.timer_lock = threading.RLock()
        self.last_api_call = 0.0
        self._rate_lock = threading.Lock()
        self.watchdog = StreamWatchdog(self, logger)
        self.watchdog.start()

        # Startup recovery: if state persisted as STREAMING but no stream
        # process is alive, don't wait for the first watchdog tick — act now.
        if self.state_machine.get()["state"] == "STREAMING":
            if not self.process_manager.is_stream_alive():
                logger.info("Startup: stale STREAMING state with no stream process — transitioning immediately")
                self.state_machine.transition("POSSIBLE_DISCONNECT")
                self._schedule_verify_disconnect()
                self._schedule_possible_disconnect_deadline()

    def verify_token(self) -> bool:
        expected = self.config.get("api_token")
        if not expected:
            return True
        return request.headers.get("X-API-Token") == expected

    def rate_limit_ok(self) -> bool:
        with self._rate_lock:
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
            self.timer_deadlines.clear()

    def _schedule_timer(self, name: str, delay: int, fn, **kwargs) -> None:
        timer = threading.Timer(delay, fn, kwargs=kwargs if kwargs else None)
        timer.daemon = True
        with self.timer_lock:
            if name in self.timers:
                self.timers[name].cancel()
            self.timers[name] = timer
            self.timer_deadlines[name] = time.time() + delay
        timer.start()

    def start_stream(self) -> dict:
        self.cancel_timers()
        self.state_machine.transition("STARTING")
        self.power_manager.set_profile("high_performance")
        self.process_manager.launch_steam()
        self.session_tracker.begin()
        self.audit("start")
        self.state_machine.transition("STREAMING")
        return self.state_machine.get()

    def end_stream(self) -> dict:
        self.cancel_timers()
        self.state_machine.transition("CLEANING")
        self.audit("end")
        threading.Thread(target=self._do_cleanup_work, kwargs={"reason": "end"}, daemon=True).start()
        return self.state_machine.get()

    def handle_disconnect(self, reason: str = "disconnect") -> dict:
        current_state = self.state_machine.get()["state"]
        self.audit("disconnect", reason=reason)

        if current_state == "STREAMING":
            self.state_machine.transition("POSSIBLE_DISCONNECT")
            self._schedule_verify_disconnect()
            self._schedule_possible_disconnect_deadline()
        elif current_state == "GRACE_PERIOD":
            self._schedule_cleanup()
        elif current_state in {"IDLE", "STARTING"}:
            self.state_machine.transition("GRACE_PERIOD")
            self._schedule_cleanup()

        return self.state_machine.get()

    def reconnect(self) -> dict:
        self.cancel_timers()
        self.audit("reconnect")
        self.state_machine.transition("STREAMING")
        return self.state_machine.get()

    def reload_config(self) -> dict:
        new_cfg = load_config()
        self.config.clear()
        self.config.update(new_cfg)
        # Re-apply watchdog settings that are cached at init time
        watchdog_cfg = new_cfg.get("watchdog", {})
        conn_cfg = new_cfg.get("connection_check", {})
        self.watchdog.interval_seconds = int(watchdog_cfg.get("interval_seconds", 5))
        self.watchdog.missing_stream_seconds = int(watchdog_cfg.get("missing_stream_seconds", 20))
        self.watchdog.connection_check_enabled = bool(conn_cfg.get("enabled", False))
        self.watchdog.streaming_ports = set(int(p) for p in conn_cfg.get("ports", [47998, 48010]))
        self.watchdog.check_interval_seconds = int(conn_cfg.get("check_interval_seconds", 600))
        self.watchdog.kill_after_seconds = int(conn_cfg.get("kill_after_seconds", 1200))
        self.power_manager._profiles = new_cfg.get("power_profiles", {})
        logger.info("Config reloaded from %s", CONFIG_PATH)
        return {"reloaded": True}

    def _schedule_verify_disconnect(self) -> None:
        delay = int(self.config.get("timers", {}).get("disconnect_verify_seconds", 8))
        self._schedule_timer("disconnect_verify", delay, self._verify_disconnect)
        logger.info("Disconnect verification scheduled in %ss", delay)

    def _verify_disconnect(self) -> None:
        if self.state_machine.get()["state"] != "POSSIBLE_DISCONNECT":
            return

        if self.process_manager.is_stream_alive():
            logger.info("False disconnect; stream process is still alive")
            self.state_machine.transition("STREAMING")
            return

        self.state_machine.transition("GRACE_PERIOD")
        self._schedule_cleanup()

    def _schedule_possible_disconnect_deadline(self) -> None:
        delay = int(self.config.get("timers", {}).get("possible_disconnect_max_seconds", 60))
        self._schedule_timer("possible_disconnect_deadline", delay, self._possible_disconnect_expire)
        logger.info("POSSIBLE_DISCONNECT deadline set at %ss", delay)

    def _possible_disconnect_expire(self) -> None:
        if self.state_machine.get()["state"] != "POSSIBLE_DISCONNECT":
            return
        logger.info("POSSIBLE_DISCONNECT deadline reached — advancing to GRACE_PERIOD")
        self.state_machine.transition("GRACE_PERIOD")
        self._schedule_cleanup()

    def _schedule_cleanup(self) -> None:
        self.cancel_timers()
        delay = int(self.config.get("timers", {}).get("cleanup_seconds", 1800))
        self._schedule_timer("cleanup", delay, self.cleanup, reason="grace_period_expired")
        logger.info("Cleanup scheduled in %ss", delay)

    def cleanup(self, reason: str) -> dict:
        """Synchronous cleanup — safe to call from background threads (watchdog, timers)."""
        self.cancel_timers()
        self.state_machine.transition("CLEANING")
        self._do_cleanup_work(reason)
        return self.state_machine.get()

    def _do_cleanup_work(self, reason: str) -> None:
        terminated_processes = self.process_manager.kill_cleanup_processes()
        high_cpu_processes = self.resource_monitor.kill_high_cpu_processes()
        self.session_tracker.end(
            reason=reason,
            terminated_processes=terminated_processes + high_cpu_processes,
        )
        self.power_manager.set_profile("balanced")
        self.audit(
            "cleanup",
            reason=reason,
            terminated_processes=terminated_processes,
            high_cpu_processes=high_cpu_processes,
        )
        self.state_machine.transition("IDLE")

    def get_status(self) -> dict:
        state = self.state_machine.get()
        now = time.time()

        timers = {}
        with self.timer_lock:
            for name, deadline in self.timer_deadlines.items():
                timers[name] = max(0, int(deadline - now))

        session_start = self.session_tracker.start_time
        return {
            **state,
            "timers_remaining_seconds": timers,
            "session_start": int(session_start) if session_start else None,
            "session_duration_seconds": int(now - session_start) if session_start else None,
            "connection_active": self.process_manager.is_connection_active(),
        }


daemon = SunshineDaemon()


_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sunshine Daemon Metrics</title>
<style>
body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0d1117;color:#c9d1d9;margin:20px;}
h1{color:#58a6ff;font-size:18px;}
table{border-collapse:collapse;width:100%;margin-top:10px;}
th,td{padding:6px 12px;text-align:right;border-bottom:1px solid #30363d;}
th{background:#161b22;color:#8b949e;text-align:left;}
td:first-child,th:first-child{text-align:left;color:#58a6ff;}
.warn{color:#f85149;font-weight:bold;}
.ok{color:#3fb950;}
.controls{margin:10px 0;}
input,select{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:4px 8px;}
.spike{background:#3d1a1a;}
</style></head>
<body>
<h1>Sunshine Daemon Metrics</h1>
<div class="controls">
  Window: <select id="window">
    <option value="60">1 min</option>
    <option value="300" selected>5 min</option>
    <option value="900">15 min</option>
    <option value="3600">1 hour</option>
  </select>
  Refresh: <select id="refresh">
    <option value="2000" selected>2s</option>
    <option value="5000">5s</option>
    <option value="10000">10s</option>
  </select>
  <span id="updated" style="color:#8b949e;margin-left:20px;"></span>
</div>
<table id="stats">
  <thead><tr><th>Operation</th><th>Count</th><th>Avg ms</th><th>P50 ms</th><th>P99 ms</th><th>Max ms</th></tr></thead>
  <tbody></tbody>
</table>
<h1 style="margin-top:30px;">Recent spikes (>20ms)</h1>
<table id="spikes">
  <thead><tr><th>Time</th><th>Operation</th><th>Duration ms</th><th>Details</th></tr></thead>
  <tbody></tbody>
</table>
<script>
let timer = null;
async function refresh() {
  const w = document.getElementById('window').value;
  const r1 = await fetch('/metrics?window=' + w).then(r => r.json());
  const r2 = await fetch('/metrics/recent?window=' + w + '&limit=200').then(r => r.json());
  renderStats(r1.stats);
  renderSpikes(r2.samples);
  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
}
function renderStats(stats) {
  const tbody = document.querySelector('#stats tbody');
  tbody.innerHTML = '';
  const ops = Object.keys(stats).sort((a, b) => stats[b].max_ms - stats[a].max_ms);
  for (const op of ops) {
    const s = stats[op];
    const cls = s.max_ms > 50 ? 'warn' : (s.max_ms > 20 ? '' : 'ok');
    tbody.insertAdjacentHTML('beforeend',
      `<tr><td>${op}</td><td>${s.count}</td><td>${s.avg_ms}</td><td>${s.p50_ms}</td><td class="${cls}">${s.p99_ms}</td><td class="${cls}">${s.max_ms}</td></tr>`);
  }
}
function renderSpikes(samples) {
  const tbody = document.querySelector('#spikes tbody');
  tbody.innerHTML = '';
  const spikes = samples.filter(s => s.ms > 20).reverse().slice(0, 50);
  for (const s of spikes) {
    const t = new Date(s.ts * 1000).toLocaleTimeString();
    const d = s.d ? JSON.stringify(s.d) : '';
    tbody.insertAdjacentHTML('beforeend',
      `<tr class="spike"><td>${t}</td><td>${s.op}</td><td>${s.ms}</td><td>${d}</td></tr>`);
  }
}
function schedule() {
  if (timer) clearInterval(timer);
  timer = setInterval(refresh, parseInt(document.getElementById('refresh').value));
}
document.getElementById('window').addEventListener('change', refresh);
document.getElementById('refresh').addEventListener('change', schedule);
refresh(); schedule();
</script>
</body></html>
"""


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


@app.route("/status", methods=["GET"])
def status_route():
    return jsonify({"ok": True, **daemon.get_status()})


@app.route("/metrics", methods=["GET"])
def metrics_route():
    window = int(request.args.get("window", 300))
    return jsonify({"ok": True, "window_seconds": window, "stats": daemon.metrics.stats(window)})


@app.route("/metrics/recent", methods=["GET"])
def metrics_recent_route():
    window = int(request.args.get("window", 300))
    op = request.args.get("op")
    limit = int(request.args.get("limit", 500))
    return jsonify({
        "ok": True,
        "window_seconds": window,
        "op": op,
        "samples": daemon.metrics.recent(window, op=op, limit=limit),
    })


@app.route("/metrics/dashboard", methods=["GET"])
def metrics_dashboard_route():
    return (_DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})


@app.route("/reload", methods=["POST"])
def reload_route():
    return guarded(daemon.reload_config)


def _handle_shutdown(signum, _frame) -> None:
    logger.info("Shutdown signal %s received — cleaning up", signum)
    try:
        state = daemon.state_machine.get()["state"]
        if state not in {"IDLE", "CLEANING"}:
            daemon.state_machine.transition("IDLE")
    except Exception:
        pass
    release_lock()
    sys.exit(0)


def main() -> None:
    acquire_lock()
    atexit.register(release_lock)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_shutdown)

    logger.info("Sunshine daemon starting on %s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT, threads=4, channel_timeout=30)


if __name__ == "__main__":
    main()
