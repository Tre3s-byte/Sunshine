import json
import logging
import time
from pathlib import Path


class SessionTracker:
    """Records each stream session as a JSON line in sessions.log."""

    def __init__(self, log_path: Path, logger: logging.Logger):
        self.log_path = log_path
        self.logger = logger
        self._start: float | None = None

    def begin(self) -> None:
        self._start = time.time()

    def end(self, reason: str, terminated_processes: list[str] | None = None) -> dict | None:
        if self._start is None:
            return None

        now = time.time()
        record = {
            "session_start": int(self._start),
            "session_end": int(now),
            "duration_seconds": int(now - self._start),
            "end_reason": reason,
            "terminated_processes": terminated_processes or [],
        }
        self._start = None

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self.logger.info(
                "Session recorded: duration=%ss reason=%s",
                record["duration_seconds"], reason,
            )
        except OSError as exc:
            self.logger.warning("Could not write session log: %s", exc)

        return record

    @property
    def start_time(self) -> float | None:
        return self._start
