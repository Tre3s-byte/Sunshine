import logging
import subprocess
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

    def _graceful_terminate(self, proc: psutil.Process, wait_seconds: int) -> None:
        """Send WM_CLOSE via taskkill, then force-kill if the process outlives the grace period."""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T"],
                capture_output=True, timeout=5,
            )
            proc.wait(timeout=wait_seconds)
        except psutil.TimeoutExpired:
            self.logger.info(
                "Process pid=%s did not exit gracefully, force-killing", proc.pid
            )
            try:
                proc.kill()
            except (psutil.Error, OSError):
                pass
        except (psutil.NoSuchProcess, OSError, subprocess.SubprocessError):
            pass

    def kill_cleanup_processes(self) -> list[str]:
        cleanup_processes = {
            self.normalize(name) for name in self.config.get("cleanup_processes", [])
        }
        protected_processes = {
            self.normalize(name) for name in self.config.get("protected_processes", [])
        }
        grace_seconds = int(self.config.get("graceful_kill_wait_seconds", 5))
        terminated = []

        if not cleanup_processes:
            self.logger.info("No cleanup_processes configured")
            return terminated

        for proc in psutil.process_iter(["pid", "name", "status"]):
            try:
                process_name = self.normalize(proc.info.get("name"))
                if process_name not in cleanup_processes:
                    continue
                if process_name in protected_processes:
                    self.logger.info("Skipping protected process: %s", process_name)
                    continue

                # Resume first so the close signal reaches the main thread
                if proc.info.get("status") == psutil.STATUS_STOPPED:
                    proc.resume()

                self._graceful_terminate(proc, wait_seconds=grace_seconds)
                terminated.append(f"{process_name}:{proc.info.get('pid')}")
                self.logger.info(
                    "Terminated cleanup process: %s pid=%s", process_name, proc.info.get("pid")
                )
            except (psutil.Error, OSError) as exc:
                self.logger.warning(
                    "Could not terminate process pid=%s: %s", proc.info.get("pid"), exc
                )

        return terminated
