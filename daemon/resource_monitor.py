import logging

import psutil


class ResourceMonitor:
    """Optional resource controls for processes explicitly configured as killable."""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger

    @staticmethod
    def normalize(name: str | None) -> str:
        return (name or "").lower().removesuffix(".exe")

    def kill_high_cpu_processes(self) -> list[str]:
        resource_config = self.config.get("resource_monitor", {})
        if not resource_config.get("enabled", False):
            return []

        threshold = float(resource_config.get("cpu_percent_threshold", 80))
        killable = {self.normalize(name) for name in resource_config.get("killable_processes", [])}
        terminated = []

        if not killable:
            return terminated

        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                process_name = self.normalize(proc.info.get("name"))
                cpu_percent = float(proc.info.get("cpu_percent") or 0)
                if process_name in killable and cpu_percent > threshold:
                    proc.terminate()
                    terminated.append(f"{process_name}:{proc.info.get('pid')}")
                    self.logger.info(
                        "Terminated high CPU process: %s pid=%s cpu=%s",
                        process_name,
                        proc.info.get("pid"),
                        cpu_percent,
                    )
            except (psutil.Error, OSError, ValueError) as exc:
                self.logger.warning("Resource monitor skipped pid=%s: %s", proc.info.get("pid"), exc)

        return terminated
