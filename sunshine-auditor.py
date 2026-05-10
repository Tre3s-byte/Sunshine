import ctypes
import json
import logging
import os
import threading
import time
import atexit
from pathlib import Path

import psutil
from flask import Flask, jsonify, request

# ============================ PATHS ============================

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "auditor.log"

LOCK_FILE = BASE_DIR / "auditor.lock"

# ============================ SINGLE INSTANCE LOCK ============================


def is_running(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


if LOCK_FILE.exists():
    try:
        old_pid = int(LOCK_FILE.read_text().strip())
        if is_running(old_pid):
            raise SystemExit("Auditor already running")
    except Exception:
        pass

LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


atexit.register(remove_lock)

# ============================ LOGGING ============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUDITOR] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("sunshine-auditor")

app = Flask(__name__)

# ============================ AUDITOR CORE ============================


class Auditor:
    def __init__(self):
        self.config = self.load_config()

        self.lock = threading.Lock()
        self.timers = {}

        self.generation = 0
        self.state = "IDLE"

        self.cleanup_running = False
        self.suspended_pids = set()
        self.last_api_call = 0

        self._ending = False

        self.watchdog_thread = threading.Thread(
            target=self.stream_watchdog, daemon=True
        )
        self.watchdog_thread.start()

        self.restore_state()

    # ============================ STATE ============================

    def transition(self, new_state: str):
        with self.lock:
            old = self.state
            if old == new_state:
                return

            self.state = new_state
            logger.info(f"STATE {old} -> {new_state}")
            self.save_state()

    def log_event(self, event: str):
        logger.info(f"EVENT {event} | state={self.state} gen={self.generation}")

    # ============================ STREAM EVENTS ============================

    def on_stream_start(self):
        self._ending = False
        self.log_event("stream_start")
        self.cancel_all_timers()
        self.resume_suspended_games()
        self.transition("STREAMING")

    def on_stream_end(self, reason="end"):
        if self._ending:
            return

        self._ending = True
        self.log_event(f"stream_end reason={reason}")

        if self.state == "STREAMING":
            self.transition("POSSIBLE_DISCONNECT")

            t = threading.Timer(8, self.verify_real_disconnect)
            t.daemon = True

            with self.lock:
                self.timers["disconnect_verify"] = t

            t.start()
        else:
            logger.info(f"stream_end ignored in state {self.state}")

    def verify_real_disconnect(self):
        if self.state != "POSSIBLE_DISCONNECT":
            return

        if self.is_stream_alive():
            logger.info("False disconnect detected")
            self.transition("STREAMING")
            self._ending = False
        else:
            self.on_stream_end_final()

    def on_stream_end_final(self):
        logger.info("Final disconnect confirmed")
        self.transition("DISCONNECTED_GRACE")
        self.schedule_cleanup_timers()

    # ============================ DISCONNECT / RECONNECT ============================

    def handle_disconnect(self):
        self.log_event("disconnect")

        if self.state == "STREAMING":
            self.on_stream_end("disconnect")
        elif self.state == "DISCONNECTED_GRACE":
            self.cancel_all_timers()
            self.schedule_cleanup_timers()
        else:
            self.on_stream_end("disconnect")

    def handle_reconnect(self):
        self.log_event("reconnect")

        self.cancel_all_timers()
        self.resume_suspended_games()
        self._ending = False

        self.transition("STREAMING")

    # ============================ TIMERS ============================

    def cancel_all_timers(self):
        with self.lock:
            for t in self.timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self.timers.clear()

    def schedule_cleanup_timers(self):
        delay = self.config.get("timers", {}).get("cleanup_seconds", 1800)

        t = threading.Timer(delay, self.run_hybrid_cleanup)
        t.daemon = True

        with self.lock:
            self.timers["cleanup"] = t

        logger.info(f"Cleanup scheduled in {delay}s")
        t.start()

    def run_hybrid_cleanup(self):
        if self.cleanup_running:
            return
        if self.state != "DISCONNECTED_GRACE":
            return

        self.cleanup_running = True

        logger.info("Running cleanup")
        self.transition("CLEANING")

        self.transition("IDLE")
        self.cleanup_running = False

    # ============================ UTILS ============================

    def rate_limit_ok(self):
        now = time.time()
        if now - self.last_api_call < 0.5:
            return False
        self.last_api_call = now
        return True

    def verify_token(self):
        expected = self.config.get("api_token")
        if not expected:
            return True
        return request.headers.get("X-API-Token") == expected

    def is_stream_alive(self):
        return bool(self.find_processes(["sunshine", "parsec", "parsecd"]))

    def stream_watchdog(self):
        last_alive = time.time()

        while True:
            time.sleep(5)

            if self.state != "STREAMING":
                continue

            if self.is_stream_alive():
                last_alive = time.time()
            elif time.time() - last_alive > 20:
                logger.info("Watchdog triggered disconnect")
                self.on_stream_end("watchdog")

    def find_processes(self, patterns):
        patterns = [p.lower() for p in patterns]
        matches = []

        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info["name"] or "").lower()
                if any(p in name for p in patterns):
                    matches.append(proc)
            except Exception:
                pass

        return matches

    def resume_suspended_games(self):
        for pid in list(self.suspended_pids):
            try:
                psutil.Process(pid).resume()
            except Exception:
                pass
        self.suspended_pids.clear()

    # ============================ STATE IO ============================

    def save_state(self):
        data = {
            "generation": self.generation,
            "state": self.state,
            "timestamp": int(time.time()),
        }

        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        os.replace(tmp, STATE_PATH)

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def restore_state(self):
        if not STATE_PATH.exists():
            return

        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved = data.get("state", "IDLE")

            if saved not in ["IDLE", "STREAMING", "DISCONNECTED_GRACE"]:
                saved = "IDLE"

            self.state = saved
            logger.info(f"Recovered state: {saved}")

        except Exception:
            self.state = "IDLE"


auditor = Auditor()

# ============================ ROUTES ============================


@app.route("/start", methods=["POST"])
def start_stream():
    if not auditor.rate_limit_ok():
        return jsonify({"error": "rate-limited"}), 429
    if not auditor.verify_token():
        return jsonify({"error": "unauthorized"}), 401

    auditor.on_stream_start()
    return jsonify({"ok": True, "state": auditor.state})


@app.route("/end", methods=["POST"])
def end_stream():
    if not auditor.rate_limit_ok():
        return jsonify({"error": "rate-limited"}), 429
    if not auditor.verify_token():
        return jsonify({"error": "unauthorized"}), 401

    auditor.on_stream_end("end")
    return jsonify({"ok": True, "state": auditor.state})


@app.route("/disconnect", methods=["POST"])
def disconnect():
    if not auditor.rate_limit_ok():
        return jsonify({"error": "rate-limited"}), 429
    if not auditor.verify_token():
        return jsonify({"error": "unauthorized"}), 401

    auditor.handle_disconnect()
    return jsonify({"ok": True, "state": auditor.state})


@app.route("/reconnect", methods=["POST"])
def reconnect():
    if not auditor.rate_limit_ok():
        return jsonify({"error": "rate-limited"}), 429
    if not auditor.verify_token():
        return jsonify({"error": "unauthorized"}), 401

    auditor.handle_reconnect()
    return jsonify({"ok": True, "state": auditor.state})


@app.route("/health")
def health():
    return jsonify({"state": auditor.state, "generation": auditor.generation})


if __name__ == "__main__":
    logger.info("Sunshine Auditor starting")

    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True, use_reloader=False)
