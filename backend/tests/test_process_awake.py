"""No test launches caffeinate or changes host power assertions."""
import subprocess
from unittest.mock import Mock

import pytest

from app.core import process_awake as module


@pytest.mark.parametrize("enabled,platform,state", [
    (False, "darwin", "disabled"), (True, "linux", "unsupported_platform"),
])
def test_disabled_and_non_mac_never_spawn(monkeypatch, enabled, platform, state):
    launch = Mock()
    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    guard = module.ProcessAwakeGuard(enabled=enabled)
    assert guard.start()["status"] == state
    launch.assert_not_called()


def test_mac_guard_is_ac_only_pid_bound_idempotent_and_reversible(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.os, "getpid", lambda: 123)
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    guard = module.ProcessAwakeGuard(enabled=True)
    assert guard.start()["status"] == "requested"
    status = guard.start()
    assert launch.call_count == 1
    assert launch.call_args.args[0] == ["/usr/bin/caffeinate", "-s", "-w", "123"]
    assert status["ac_only"] and not status["lid_sleep_protected"]
    assert not status["manual_sleep_protected"] and not status["system_settings_changed"]
    status["process_running"] = False
    assert guard.status()["process_running"] is True
    guard.stop()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=1)
    guard.stop()
    assert process.terminate.call_count == 1
    assert guard.status()["status"] == "stopped"


def test_launch_failure_and_early_exit_are_not_reported_as_protection(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.subprocess, "Popen", Mock(side_effect=FileNotFoundError()))
    guard = module.ProcessAwakeGuard(enabled=True)
    assert guard.start()["status"] == "failed"
    assert guard.status()["process_running"] is False
    process = Mock()
    process.poll.return_value = 1
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    assert guard.start()["status"] == "exited"
    assert guard.status()["exit_code"] == 1


def test_shutdown_uses_bounded_terminate_then_kill(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0]
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    guard = module.ProcessAwakeGuard(enabled=True)
    guard.start()
    guard.stop()
    process.kill.assert_called_once()
    assert guard.status()["status"] == "stopped"


def test_scheduler_stop_cleans_guard_even_if_already_stopped(monkeypatch):
    from app.data.scheduler import DataScheduler
    scheduler = DataScheduler()
    stop = Mock()
    monkeypatch.setattr(scheduler._process_awake_guard, "stop", stop)
    scheduler.stop()
    stop.assert_called_once()
    assert scheduler.get_pipeline_runtime_status()["process_awake"]["lid_sleep_protected"] is False
