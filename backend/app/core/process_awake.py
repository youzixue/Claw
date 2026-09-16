"""Process-owned macOS AC sleep assertion; never changes system power settings."""

from __future__ import annotations

import os
import subprocess
import sys

from loguru import logger


class ProcessAwakeGuard:
    """Hold an AC-only assertion while the scheduler runs.

    Does not prevent lid-closed sleep, manual sleep, shutdown or loss of power.
    A running caffeinate process is not proof that every sleep cause is blocked.
    """

    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self._process: subprocess.Popen | None = None
        self._state = "not_started"
        self._error: str | None = None

    def start(self) -> dict:
        if not self.enabled:
            self._state = "disabled"
            return self.status()
        if sys.platform != "darwin":
            self._state = "unsupported_platform"
            return self.status()
        if self._process is not None and self._process.poll() is None:
            return self.status()
        try:
            self._process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
            self._state = "requested"
            self._error = None
            logger.info("已请求Claw进程级AC防休眠；合盖/手动休眠仍会中断行情，电池供电不保护")
        except OSError as exc:
            self._process = None
            self._state = "failed"
            self._error = type(exc).__name__
            logger.warning("Claw进程级防休眠请求失败: {}", self._error)
        return self.status()

    def stop(self) -> None:
        process = self._process
        try:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                else:
                    process.wait(timeout=1)
            self._process = None
            self._state = "stopped"
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Retain the handle on cleanup failure; parent PID binding also
            # releases the assertion if the owning backend exits.
            self._state = "stop_failed"
            self._error = type(exc).__name__
            logger.warning("Claw防休眠子进程回收失败: {}", self._error)

    def status(self) -> dict:
        code = self._process.poll() if self._process is not None else None
        running = self._process is not None and code is None
        state = "exited" if self._process is not None and not running else self._state
        return {
            "policy": "macos_ac_process_assertion_v1",
            "enabled": self.enabled, "status": state,
            "process_running": running, "exit_code": code, "error_type": self._error,
            "ac_only": True, "lid_sleep_protected": False,
            "manual_sleep_protected": False, "system_settings_changed": False,
        }
