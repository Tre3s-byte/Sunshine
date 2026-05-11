import logging
import threading
import time


class StreamWatchdog:
    """Background watchdog that confirms real disconnects before cleanup.

    Two independent checks run every interval while state is STREAMING:

    1. Process check — if the stream process disappears for longer than
       *missing_stream_seconds* a disconnect is triggered (existing behaviour).

    2. Connection check (opt-in via config "connection_check.enabled") — if no
       ESTABLISHED TCP connection is seen on the configured streaming ports for
       longer than *missing_connection_seconds*, a disconnect is triggered even
       when the stream process is still running.  This catches internet drops,
       Moonlight being minimised without a proper close, or any other situation
       where the OS-level connection silently vanishes.

    Both counters are reset whenever the watchdog re-enters STREAMING state so
    that a reconnect() after a POSSIBLE_DISCONNECT never triggers a spurious
    immediate disconnect.
    """

    def __init__(self, daemon, logger: logging.Logger):
        self.daemon = daemon
        self.logger = logger

        watchdog_cfg = daemon.config.get("watchdog", {})
        conn_cfg = daemon.config.get("connection_check", {})

        self.interval_seconds = int(watchdog_cfg.get("interval_seconds", 5))
        self.missing_stream_seconds = int(watchdog_cfg.get("missing_stream_seconds", 20))

        self.connection_check_enabled = bool(conn_cfg.get("enabled", False))
        self.streaming_ports = set(int(p) for p in conn_cfg.get("ports", [47998, 48010]))
        self.missing_connection_seconds = int(conn_cfg.get("missing_connection_seconds", 30))

        self.thread = threading.Thread(target=self.run, daemon=True, name="stream-watchdog")

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        last_seen = time.time()
        last_connected = time.time()
        prev_state = None

        while True:
            time.sleep(self.interval_seconds)
            state = self.daemon.state_machine.get()["state"]

            if state != "STREAMING":
                prev_state = state
                continue

            # Reset staleness counters when re-entering STREAMING (e.g. after reconnect)
            if prev_state != "STREAMING":
                last_seen = time.time()
                last_connected = time.time()
                self.logger.info("Watchdog: entered STREAMING, counters reset")
            prev_state = state

            # --- Process check ---
            if self.daemon.process_manager.is_stream_alive():
                last_seen = time.time()
            elif time.time() - last_seen > self.missing_stream_seconds:
                self.logger.info(
                    "Watchdog: stream process absent for >%ss — triggering disconnect",
                    self.missing_stream_seconds,
                )
                self.daemon.handle_disconnect(reason="watchdog_process_gone")
                continue

            # --- Connection check (optional) ---
            if not self.connection_check_enabled:
                continue

            if self.daemon.process_manager.is_connection_active(self.streaming_ports):
                last_connected = time.time()
            else:
                elapsed = time.time() - last_connected
                self.logger.debug(
                    "Watchdog: no active connection on ports %s (%.0fs elapsed)",
                    self.streaming_ports,
                    elapsed,
                )
                if elapsed > self.missing_connection_seconds:
                    self.logger.info(
                        "Watchdog: no active connection for >%ss — triggering disconnect",
                        self.missing_connection_seconds,
                    )
                    self.daemon.handle_disconnect(reason="watchdog_connection_lost")
