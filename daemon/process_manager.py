import contextlib
import ctypes
import logging
import platform
import subprocess
import time
from collections.abc import Iterable

import psutil

from daemon.metrics import MetricsTracker


_SUNSHINE_STREAMING_PORTS = {47998, 48010}
_IS_WINDOWS = platform.system() == "Windows"


if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_ACCESS_DENIED = 5
    _STILL_ACTIVE = 259

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    def _pid_alive(pid: int) -> bool:
        """Check PID liveness via Win32 OpenProcess + GetExitCodeProcess.

        Single Win32 call per PID — no EnumProcesses, no Toolhelp32 snapshot,
        no system-wide process table walk. This is the lightest possible
        liveness check on Windows and avoids the kernel hooks that
        anti-cheat systems (NTE, EAC, BattlEye) flag as suspicious.

        Returns True if the process is running. ACCESS_DENIED means the
        process exists but our token can't open it (e.g. another user's
        process or a protected process), still counts as alive.
        """
        if pid <= 0:
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == _STILL_ACTIVE
            return True
        finally:
            _kernel32.CloseHandle(handle)
else:
    def _pid_alive(pid: int) -> bool:
        return psutil.pid_exists(pid)


@contextlib.contextmanager
def _maybe_time(metrics: MetricsTracker | None, op: str, **details):
    if metrics is None:
        yield
    else:
        with metrics.time_call(op, **details):
            yield


class ProcessManager:
    """Process lookup and cleanup helpers used only by the daemon."""

    def __init__(self, config: dict, logger: logging.Logger, metrics: MetricsTracker | None = None):
        self.config = config
        self.logger = logger
        self.metrics = metrics

    @staticmethod
    def normalize(name: str | None) -> str:
        name = (name or "").lower()
        return name.removesuffix(".exe")

    def find_by_patterns(self, patterns: Iterable[str]) -> list[psutil.Process]:
        normalized_patterns = [self.normalize(pattern) for pattern in patterns]
        matches = []

        with _maybe_time(self.metrics, "process_iter", caller="find_by_patterns"):
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

    def is_stream_alive_cached(self, cached_pids: set[int]) -> tuple[bool, set[int]]:
        """Check stream liveness without a full process scan on the hot path.

        Fast path: verify cached PIDs via Win32 OpenProcess — single syscall
        per PID, no EnumProcesses, no Toolhelp32 snapshot. This is the
        anti-cheat-safe replacement for psutil.pid_exists() (which on some
        psutil/Windows builds enumerates the full process table).

        Slow path (full scan): only triggered when every cached PID is gone,
        meaning the previous stream process actually died. Returns fresh PIDs
        so the next call stays on the fast path.
        """
        if cached_pids:
            with _maybe_time(self.metrics, "pid_alive_cached", n=len(cached_pids)):
                alive = {pid for pid in cached_pids if _pid_alive(pid)}
            if alive:
                return True, alive

        # Full scan only when cache is cold or all cached PIDs are dead
        stream_processes = self.config.get(
            "stream_processes", ["sunshine", "moonlight", "parsec", "parsecd"]
        )
        found = self.find_by_patterns(stream_processes)
        new_pids = {proc.pid for proc in found}
        return bool(new_pids), new_pids

    def is_connection_active(self, ports: Iterable[int] | None = None, stream_pids: set[int] | None = None) -> bool:
        """Return True if Sunshine has an active client connection.

        Only TCP ESTABLISHED connections are considered. UDP sockets are
        excluded because Sunshine keeps them bound with a cached raddr after
        the client disconnects, which causes false positives.

        Primary check (when stream_pids is provided): inspects the TCP socket
        table of known stream processes, filtered to streaming ports. The local
        port (laddr) is always the Sunshine streaming port regardless of VPN
        tunneling (Tailscale/WireGuard), so port filtering is safe here and
        prevents false positives from Sunshine's web UI or other connections.

        Fallback (port-based): used when no PIDs are cached yet.
        """
        try:
            with _maybe_time(self.metrics, "net_connections", scoped=bool(stream_pids)):
                conns = psutil.net_connections(kind="tcp")

            port_set = set(ports) if ports is not None else _SUNSHINE_STREAMING_PORTS

            if stream_pids:
                for conn in conns:
                    if conn.pid not in stream_pids:
                        continue
                    if conn.status != "ESTABLISHED":
                        continue
                    if not conn.raddr:
                        continue
                    # Filter by local streaming port to exclude Sunshine's web UI
                    # and other non-streaming connections that would be false positives.
                    if conn.laddr and conn.laddr.port not in port_set:
                        continue
                    return True
                return False

            # Fallback: port-based scan (no PID info available yet)
            for conn in conns:
                lport = conn.laddr.port if conn.laddr else None
                if lport not in port_set:
                    continue
                if conn.status == "ESTABLISHED" and conn.raddr:
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

        with _maybe_time(self.metrics, "process_iter", caller="suspend_games"):
            iterator = list(psutil.process_iter(["pid", "name", "status"]))

        for proc in iterator:
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

        with _maybe_time(self.metrics, "process_iter", caller="resume_games"):
            iterator = list(psutil.process_iter(["pid", "name", "status"]))

        for proc in iterator:
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
        with _maybe_time(self.metrics, "process_iter", caller="kill_cleanup"):
            iterator = list(psutil.process_iter(["pid", "name", "status"]))

        for proc in iterator:
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
                subprocess.Popen(
                    ["taskkill", "/PID", str(proc.pid), "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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
