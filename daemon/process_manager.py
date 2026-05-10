import logging
from collections.abc import Iterable

import psutil


class ProcessManager:
    """Process lookup and cleanup helpers used only by the daemon."""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger

    @staticmethod
    def normalize(name: str | None) -> str:
        name = (name or "").lower()
        return name.removesuffix(".exe")

    def find_by_patterns(self, patterns: Iterable[str]) -> list[psutil.Process]:
        normalized_patterns = [self.normalize(pattern) for pattern in patterns]
        matches = []

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                process_name = self.normalize(proc.info.get("name"))
                if any(pattern in process_name for pattern in normalized_patterns):
                    matches.append(proc)
            except (psutil.Error, OSError):
                continue

        return matches

    def is_stream_alive(self) -> bool:
        stream_processes = self.config.get(
            "stream_processes", ["sunshine", "moonlight", "parsec", "parsecd"]
        )
        return bool(self.find_by_patterns(stream_processes))

    def kill_cleanup_processes(self) -> list[str]:
        cleanup_processes = {
            self.normalize(name) for name in self.config.get("cleanup_processes", [])
        }
        protected_processes = {
            self.normalize(name) for name in self.config.get("protected_processes", [])
        }
        terminated = []

        if not cleanup_processes:
            self.logger.info("No cleanup_processes configured")
            return terminated

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                process_name = self.normalize(proc.info.get("name"))
                if process_name not in cleanup_processes:
                    continue
                if process_name in protected_processes:
                    self.logger.info("Skipping protected process: %s", process_name)
                    continue

                proc.terminate()
                terminated.append(f"{process_name}:{proc.info.get('pid')}")
                self.logger.info("Terminated cleanup process: %s pid=%s", process_name, proc.info.get("pid"))
            except (psutil.Error, OSError) as exc:
                self.logger.warning("Could not terminate process pid=%s: %s", proc.info.get("pid"), exc)

        return terminated
