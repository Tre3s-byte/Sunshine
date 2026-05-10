import logging
import threading
import time


class StreamWatchdog:
    """Background watchdog that confirms real disconnects before cleanup."""

    def __init__(self, daemon, logger: logging.Logger):
        self.daemon = daemon
        self.logger = logger
        self.interval_seconds = int(daemon.config.get("watchdog", {}).get("interval_seconds", 5))
        self.missing_stream_seconds = int(
            daemon.config.get("watchdog", {}).get("missing_stream_seconds", 20)
        )
        self.thread = threading.Thread(target=self.run, daemon=True, name="stream-watchdog")

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        last_seen = time.time()

        while True:
            time.sleep(self.interval_seconds)
            state = self.daemon.state_machine.get()["state"]
            if state != "STREAMING":
                continue

            if self.daemon.process_manager.is_stream_alive():
                last_seen = time.time()
                continue

            if time.time() - last_seen > self.missing_stream_seconds:
                self.logger.info("Watchdog detected possible disconnect")
                self.daemon.handle_disconnect(reason="watchdog")
