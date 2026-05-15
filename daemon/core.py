import json
import logging
import os
import threading
import time
from pathlib import Path


VALID_STATES = [
    "IDLE",
    "STARTING",
    "STREAMING",
    "POSSIBLE_DISCONNECT",
    "GRACE_PERIOD",
    "CLEANING",
]


class StateMachine:
    """Thread-safe, JSON-persisted system state for the Sunshine daemon."""

    def __init__(self, state_path: Path, logger: logging.Logger):
        self.state_path = state_path
        self.logger = logger
        self.lock = threading.RLock()
        self.generation = 0
        self.state = "IDLE"
        self.state_entered_at = int(time.time())
        self.restore()

    def get(self) -> dict:
        with self.lock:
            return {
                "generation": self.generation,
                "state": self.state,
                "timestamp": int(time.time()),
                "state_entered_at": self.state_entered_at,
            }

    def transition(self, new_state: str) -> str:
        if new_state not in VALID_STATES:
            raise ValueError(f"Invalid state: {new_state}")

        with self.lock:
            old_state = self.state
            if old_state == new_state:
                return self.state

            self.state = new_state
            self.state_entered_at = int(time.time())
            self.generation += 1
            self.logger.info("STATE %s -> %s", old_state, new_state)
            self.save_locked()
            return self.state

    def save_locked(self) -> None:
        data = {
            "generation": self.generation,
            "state": self.state,
            "timestamp": int(time.time()),
            "state_entered_at": self.state_entered_at,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def restore(self) -> None:
        if not self.state_path.exists():
            return

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = data.get("state", "IDLE")
            if state not in VALID_STATES:
                state = "IDLE"

            with self.lock:
                self.state = state
                # Older state files only have timestamp; use it as the best
                # available state-entry time so grace-period cleanup deadlines
                # survive daemon restarts instead of restarting from scratch.
                entered_at = data.get("state_entered_at", data.get("timestamp"))
                self.state_entered_at = int(entered_at or time.time())
                self.generation = int(data.get("generation", 0))
                self.logger.info("Recovered state: %s", self.state)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            with self.lock:
                self.state = "IDLE"
                self.state_entered_at = int(time.time())
                self.generation = 0
                self.logger.warning("Could not restore state; defaulting to IDLE")
