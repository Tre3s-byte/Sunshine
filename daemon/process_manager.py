import logging
import subprocess
import time
from collections.abc import Iterable

import psutil


_SUNSHINE_STREAMING_PORTS = {47998, 48010}


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

    def is_connection_active(self, ports: Iterable[int] | None = None) -> bool:
        """Return True if there is at least one ESTABLISHED connection on the given ports.

        Falls back to the default Sunshine streaming ports when *ports* is None.
        Checks both local and remote address ports to cover outbound clients.
        """
        port_set = set(ports) if ports is not None else _SUNSHINE_STREAMING_PORTS
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status != "ESTABLISHED":
                    continue
                lport = conn.laddr.port if conn.laddr else None
                rport = conn.raddr.port if conn.raddr else None
                if lport in port_set or rport in port_set:
                    return True
        except (psutil.Error, OSError) as exc:
            self.logger.warning("Connection check failed: %s", exc)
        return False

    def launch_steam(self) -> bool:
        """Start Steam if it is not already running."""
        steam_cfg = self.config.get("steam", {})
        path = steam_cfg.get("path")
        if not path:
            return False

        if self.find_by_patterns(["steam"]):
            self.logger.info("Steam already running, skipping launch")
            return True

        args = steam_cfg.get("args", [])
        try:
            subprocess.Popen([path] + args)
            self.logger.info("Launched Steam: %s %s", path, args)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.warning("Could not launch Steam: %s", exc)
            return False

    def suspend_games(self) -> list[str]:
        """Freeze processes listed in suspend_games, freeing CPU/GPU cycles without losing state.

        instant_kill_games are hard-killed immediately since they cannot be safely suspended
        (e.g. anti-cheat titles that detect suspension as tampering).
        """
        suspend_targets = {self.normalize(n) for n in self.config.get("suspend_games", [])}
        instant_kill_targets = {self.normalize(n) for n in self.config.get("instant_kill_games", [])}
        protected = {self.normalize(n) for n in self.config.get("protected_processes", [])}
        affected = []

        for proc in psutil.process_iter(["pid", "name", "status"]):
            try:
                name = self.normalize(proc.info.get("name"))
                if name in protected:
                    continue

                if name in instant_kill_targets:
                    proc.kill()
                    affected.append(f"killed:{name}:{proc.info.get('pid')}")
                    self.logger.info("Instant-killed game: %s pid=%s", name, proc.info.get("pid"))

                elif name in suspend_targets:
                    if proc.info.get("status") == psutil.STATUS_STOPPED:
                        continue
                    proc.suspend()
                    affected.append(f"suspended:{name}:{proc.info.get('pid')}")
                    self.logger.info("Suspended game: %s pid=%s", name, proc.info.get("pid"))

            except (psutil.Error, OSError) as exc:
                self.logger.warning("Could not act on pid=%s: %s", proc.info.get("pid"), exc)

        return affected

    def resume_games(self) -> list[str]:
        """Resume previously suspended game processes."""
        suspend_targets = {self.normalize(n) for n in self.config.get("suspend_games", [])}
        resumed = []

        for proc in psutil.process_iter(["pid", "name", "status"]):
            try:
                name = self.normalize(proc.info.get("name"))
                if name not in suspend_targets:
                    continue
                if proc.info.get("status") != psutil.STATUS_STOPPED:
                    continue
                proc.resume()
                resumed.append(f"{name}:{proc.info.get('pid')}")
                self.logger.info("Resumed game: %s pid=%s", name, proc.info.get("pid"))
            except (psutil.Error, OSError) as exc:
                self.logger.warning("Could not resume pid=%s: %s", proc.info.get("pid"), exc)

        return resumed

    def kill_cleanup_processes(self) -> list[str]:
        cleanup_set = {self.normalize(n) for n in self.config.get("cleanup_processes", [])}
        protected_set = {self.normalize(n) for n in self.config.get("protected_processes", [])}
        grace_seconds = int(self.config.get("graceful_kill_wait_seconds", 3))

        if not cleanup_set:
            self.logger.info("No cleanup_processes configured")
            return []

        # Collect targets, resuming any that were suspended
        targets: list[tuple[psutil.Process, str]] = []
        for proc in psutil.process_iter(["pid", "name", "status"]):
            try:
                name = self.normalize(proc.info.get("name"))
                if name not in cleanup_set or name in protected_set:
                    continue
                if proc.info.get("status") == psutil.STATUS_STOPPED:
                    proc.resume()
                targets.append((proc, name))
            except (psutil.Error, OSError):
                continue

        if not targets:
            return []

        # Phase 1 — send WM_CLOSE to all simultaneously (non-blocking)
        for proc, _ in targets:
            try:
                subprocess.Popen(["taskkill", "/PID", str(proc.pid), "/T"], capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass

        # Phase 2 — wait for all within a single shared grace window
        deadline = time.time() + grace_seconds
        for proc, _ in targets:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                pass

        # Phase 3 — force-kill survivors, record everything
        terminated = []
        for proc, name in targets:
            pid = proc.pid
            try:
                if proc.is_running():
                    proc.kill()
                    self.logger.info("Force-killed: %s pid=%s", name, pid)
            except (psutil.NoSuchProcess, OSError):
                pass
            terminated.append(f"{name}:{pid}")
            self.logger.info("Terminated cleanup process: %s pid=%s", name, pid)

        return terminated
