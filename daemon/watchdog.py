import logging
import threading
import time


class StreamWatchdog:
    """Background watchdog that monitors the stream process and network connection.

    Process check (every *interval_seconds*, default 5 s):
        If the stream process is gone for longer than *missing_stream_seconds*
        a disconnect is triggered.

    Connection check (every *check_interval_seconds*, default 600 s / 10 min):
        Runs independently of the process check on its own longer cadence.

        • Connection absent for the first time:
            – suspend_games processes are frozen (CPU/GPU freed, state preserved).
            – instant_kill_games processes are hard-killed immediately.
            Logged as "games_suspended".

        • Connection restored before the kill deadline:
            – All suspended games are resumed.
            Logged as "connection_restored".

        • Connection still absent after *kill_after_seconds* (default 1200 s / 20 min):
            – Full cleanup is triggered (terminates all cleanup_processes).
            Logged as "connection_timeout_kill".

    Both staleness counters are reset whenever the watchdog re-enters STREAMING
    state, preventing a spurious immediate trigger after a reconnect().
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
        self.check_interval_seconds = int(conn_cfg.get("check_interval_seconds", 600))
        self.kill_after_seconds = int(conn_cfg.get("kill_after_seconds", 1200))

        self.thread = threading.Thread(target=self.run, daemon=True, name="stream-watchdog")

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        last_seen = time.time()
        last_connection_check = time.time()
        last_connected = time.time()
        suspended_at: float | None = None
        prev_state: str | None = None
        cached_stream_pids: set[int] = set()

        while True:
            try:
                time.sleep(self.interval_seconds)
                state = self.daemon.state_machine.get()["state"]

                if state != "STREAMING":
                    prev_state = state
                    continue

                # Reset all counters on re-entry into STREAMING (e.g. after reconnect())
                if prev_state != "STREAMING":
                    now = time.time()
                    last_seen = now
                    last_connection_check = now
                    last_connected = now
                    cached_stream_pids = set()

                    if self.connection_check_enabled and suspended_at is not None:
                        resumed = self.daemon.process_manager.resume_games()
                        if resumed:
                            self.daemon.audit("connection_restored", resumed=resumed, reason="restream")
                        suspended_at = None

                    self.logger.info("Watchdog: entered STREAMING, all counters reset")
                prev_state = state

                # ------------------------------------------------------------------
                # Process check — uses cached PIDs to avoid full process enumeration
                # on every tick (reduces anti-cheat interference in sensitive games)
                # ------------------------------------------------------------------
                is_alive, cached_stream_pids = self.daemon.process_manager.is_stream_alive_cached(
                    cached_stream_pids
                )
                if is_alive:
                    last_seen = time.time()
                elif time.time() - last_seen > self.missing_stream_seconds:
                    self.logger.info(
                        "Watchdog: stream process absent for >%ss — triggering disconnect",
                        self.missing_stream_seconds,
                    )
                    self.daemon.handle_disconnect(reason="watchdog_process_gone")
                    continue

                # ------------------------------------------------------------------
                # Connection check — runs every check_interval_seconds (10 min)
                # ------------------------------------------------------------------
                if not self.connection_check_enabled:
                    continue

                now = time.time()
                if now - last_connection_check < self.check_interval_seconds:
                    continue
                last_connection_check = now

                if self.daemon.process_manager.is_connection_active(self.streaming_ports):
                    last_connected = now
                    if suspended_at is not None:
                        self.logger.info("Watchdog: connection restored — resuming games")
                        resumed = self.daemon.process_manager.resume_games()
                        self.daemon.audit("connection_restored", resumed=resumed)
                        suspended_at = None
                else:
                    elapsed_since_connected = now - last_connected
                    self.logger.info(
                        "Watchdog: no active connection on ports %s (%.0fs since last seen)",
                        self.streaming_ports,
                        elapsed_since_connected,
                    )

                    if suspended_at is None:
                        self.logger.info("Watchdog: suspending games after connection loss")
                        affected = self.daemon.process_manager.suspend_games()
                        self.daemon.audit("games_suspended", reason="connection_lost", processes=affected)
                        suspended_at = now

                    elif now - suspended_at > self.kill_after_seconds:
                        self.logger.info(
                            "Watchdog: no connection for >%ss since suspension — killing processes",
                            self.kill_after_seconds,
                        )
                        self.daemon.audit(
                            "connection_timeout_kill",
                            suspended_for_seconds=int(now - suspended_at),
                        )
                        self.daemon.cleanup(reason="connection_timeout")
                        suspended_at = None
                    else:
                        remaining = self.kill_after_seconds - (now - suspended_at)
                        self.logger.info(
                            "Watchdog: games still suspended, %.0fs until kill deadline",
                            remaining,
                        )

            except Exception as exc:
                self.logger.warning("Watchdog tick error (will retry next interval): %s", exc, exc_info=True)
