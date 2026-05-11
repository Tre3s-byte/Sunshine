import logging
import subprocess


class PowerManager:
    """Switches Windows power plans via powercfg on stream start/stop."""

    def __init__(self, config: dict, logger: logging.Logger):
        self._profiles = config.get("power_profiles", {})
        self.logger = logger

    def set_profile(self, name: str) -> bool:
        guid = self._profiles.get(name)
        if not guid:
            self.logger.warning("Power profile '%s' not configured", name)
            return False
        try:
            subprocess.run(
                ["powercfg", "/setactive", guid],
                check=True, capture_output=True, timeout=10,
            )
            self.logger.info("Power profile -> %s (%s)", name, guid)
            return True
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("Could not set power profile '%s': %s", name, exc)
            return False
