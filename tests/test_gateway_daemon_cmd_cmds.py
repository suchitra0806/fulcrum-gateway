"""Per-module gateway command tests: gateway_daemon_cmd (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_daemon_cmd."""

from __future__ import annotations

import os
import signal
import socket
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_daemon_cmd as _gw_daemon
from ax_cli.commands import gateway_ui as _gw_ui
from ax_cli.main import app
from tests.gateway_cmd_testlib import _seed_real_session, _strip

runner = CliRunner()


def test_gateway_run_refuses_second_live_daemon(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    gateway_core.write_gateway_pid(4242)
    monkeypatch.setattr(gateway_core, "_pid_alive", lambda pid: pid == 4242)

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 4242)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pid"] == 4242


def test_gateway_run_refuses_process_table_daemon_when_pid_file_missing(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gateway_core, "_scan_gateway_process_pids", lambda: [5514])

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 5514)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pids"] == [5514]


def test_gateway_start_launches_background_daemon_and_ui(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )

    state = {"daemon_pid": None, "ui_pid": None}
    spawned: list[tuple[list[str], str]] = []

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        spawned.append((command, str(log_path)))
        if "run" in command:
            state["daemon_pid"] = 5514
            return _FakeProcess(5514)
        state["ui_pid"] = 5515
        return _FakeProcess(5515)

    monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(_gw_daemon, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        _gw_daemon,
        "ui_status",
        lambda: {
            "running": True,
            "pid": state["ui_pid"],
            "host": "127.0.0.1",
            "port": 8765,
            "url": "http://127.0.0.1:8765",
            "log_path": str(gateway_core.ui_log_path()),
        },
    )
    opened: list[str] = []
    monkeypatch.setattr(_gw_daemon.webbrowser, "open_new_tab", lambda url: opened.append(url))

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "daemon    = started" in result.output
    assert "ui        = started" in result.output
    assert len(spawned) == 2
    assert "gateway" in spawned[0][0] and "run" in spawned[0][0]
    assert spawned[0][0][-2:] == ["--poll-interval", "1.0"]
    assert "gateway" in spawned[1][0] and "ui" in spawned[1][0]
    assert opened == []


def test_gateway_cli_argv_prefers_current_ax_script(monkeypatch, tmp_path):
    current_ax = tmp_path / "bin" / "ax"
    current_ax.parent.mkdir(parents=True)
    current_ax.write_text("#!/bin/sh\n")
    current_ax.chmod(0o755)

    monkeypatch.setattr(_gw_daemon.sys, "argv", [str(current_ax), "gateway", "start"])
    monkeypatch.setattr(_gw_daemon.sys, "executable", "/opt/homebrew/bin/python3")
    monkeypatch.setattr(_gw_daemon.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")

    argv = _gw_daemon._gateway_cli_argv("gateway", "run")

    assert argv == [str(current_ax.resolve()), "gateway", "run"]


def test_gateway_start_without_login_starts_ui_only(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))

    state = {"ui_pid": None}
    spawned: list[list[str]] = []

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        spawned.append(command)
        state["ui_pid"] = 6615
        return _FakeProcess(6615)

    monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: None)
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        _gw_daemon,
        "ui_status",
        lambda: {
            "running": True,
            "pid": state["ui_pid"],
            "host": "127.0.0.1",
            "port": 8765,
            "url": "http://127.0.0.1:8765",
            "log_path": str(gateway_core.ui_log_path()),
        },
    )

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Gateway is not logged in yet" in result.output
    assert len(spawned) == 1
    assert "gateway" in spawned[0] and "ui" in spawned[0]


def test_gateway_stop_terminates_daemon_and_ui(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_gw_daemon, "active_gateway_pids", lambda: [7714])
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pids", lambda: [7715])
    monkeypatch.setattr(
        _gw_daemon,
        "_terminate_pids",
        lambda pids, timeout=3.0: (list(pids), [pids[0]] if pids and pids[0] == 7714 else []),
    )

    result = runner.invoke(app, ["gateway", "stop"])

    assert result.exit_code == 0, result.output
    assert "daemon = [7714]" in result.output
    assert "ui     = [7715]" in result.output
    assert "Forced kill:" in result.output


def test_gateway_start_rolls_back_daemon_when_ui_start_fails(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )

    state = {"daemon_pid": None, "ui_pid": None}

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        if "run" in command:
            state["daemon_pid"] = 8814
            return _FakeProcess(8814)
        state["ui_pid"] = 8815
        return _FakeProcess(8815)

    terminated: list[list[int]] = []
    cleared: list[int | None] = []

    monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(_gw_daemon, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: False)
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(_gw_daemon, "_tail_log_lines", lambda path, lines=12: "address already in use")
    monkeypatch.setattr(
        _gw_daemon, "_terminate_pids", lambda pids, timeout=3.0: terminated.append(list(pids)) or (list(pids), [])
    )
    monkeypatch.setattr(gateway_core, "clear_gateway_pid", lambda pid=None: cleared.append(pid))

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 1, result.output
    assert "Failed to start Gateway UI." in result.output
    assert terminated == [[8814]]
    assert cleared == [None]


def test_gateway_watch_once_renders_dashboard(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["gateway"].update(
        {
            "gateway_id": "gw-12345678",
            "desired_state": "running",
            "effective_state": "running",
            "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry["agents"] = [
        {
            "name": "echo-bot",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "backlog_depth": 2,
            "processed_count": 7,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("message_received", entry=registry["agents"][0], message_id="msg-1")

    result = runner.invoke(app, ["gateway", "watch", "--once"])

    assert result.exit_code == 0, result.output
    assert "Gateway Overview" in result.output
    assert "Managed Agents" in result.output
    assert "@echo-bot" in result.output
    assert "Recent Activity" in result.output


class TestTailLogLines:
    def test_nonexistent_file(self, tmp_path):
        assert _gw_daemon._tail_log_lines(tmp_path / "nope.log") == ""

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        assert _gw_daemon._tail_log_lines(p) == ""

    def test_returns_last_lines(self, tmp_path):
        p = tmp_path / "test.log"
        p.write_text("\n".join(f"line-{i}" for i in range(20)))
        result = _gw_daemon._tail_log_lines(p, lines=3)
        assert "line-17" in result
        assert "line-18" in result
        assert "line-19" in result
        assert "line-0" not in result

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "test.log"
        p.write_text("first\n\n\nsecond\n\n")
        result = _gw_daemon._tail_log_lines(p, lines=5)
        assert "first" in result
        assert "second" in result


class TestGatewayStopCommand:
    def test_already_stopped(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "active_gateway_pids", lambda: [])
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pids", lambda: [])
        monkeypatch.setattr(_gw_daemon, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(_gw_daemon.gateway_core, "clear_gateway_pid", lambda: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0
        assert "already stopped" in _strip(result.output).lower()

    def test_stops_processes(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "active_gateway_pids", lambda: [9999])
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pids", lambda: [9998])
        monkeypatch.setattr(_gw_daemon, "_terminate_pids", lambda pids, **kw: (pids, []))
        monkeypatch.setattr(_gw_daemon, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(_gw_daemon.gateway_core, "clear_gateway_pid", lambda: None)
        monkeypatch.setattr(_gw_daemon, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0


class TestGatewayStartCommand:
    def test_no_session_daemon_not_started_ui_starts(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: None)
        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 9999
        fake_proc.poll.return_value = None
        monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda proc, **kw: True)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: 9999)
        monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": False, "url": None})
        monkeypatch.setattr(_gw_daemon, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0
        assert "not logged in" in _strip(result.output).lower() or "not started" in _strip(result.output).lower()

    def test_ui_already_running(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: 1111)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: 2222)
        monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": True, "url": "http://localhost:8765"})
        monkeypatch.setattr(_gw_daemon, "daemon_log_path", lambda: Path("/tmp/gw.log"))
        monkeypatch.setattr(_gw_daemon, "ui_log_path", lambda: Path("/tmp/ui.log"))
        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0


class TestGatewayWatchCommand:
    def test_once(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": False,
            "daemon": {"running": False, "pid": None},
            "ui": {"running": False, "pid": None, "url": None},
            "base_url": None,
            "space_id": None,
            "space_name": None,
            "user": None,
            "agents": [],
            "recent_activity": [],
            "summary": {
                "managed_agents": 0,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        monkeypatch.setattr(_gw_daemon, "_status_payload", lambda **kw: payload)
        monkeypatch.setattr(_gw_daemon, "_render_gateway_dashboard", lambda p: "dashboard")
        result = runner.invoke(app, ["gateway", "watch", "--once"])
        assert result.exit_code == 0


class TestGatewayRunCommand:
    def test_no_session(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: None)
        result = runner.invoke(app, ["gateway", "run"])
        assert result.exit_code != 0

    def test_runtime_error(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "_load_gateway_session_or_exit", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(_gw_daemon, "gateway_dir", lambda: Path("/tmp/gw"))

        class FakeDaemon:
            def __init__(self, **kw):
                pass

            def run(self, once=False):
                raise RuntimeError("lock held")

            def stop(self):
                pass

        monkeypatch.setattr(_gw_daemon, "GatewayDaemon", FakeDaemon)
        result = runner.invoke(app, ["gateway", "run"])
        assert result.exit_code != 0


class TestGatewayCliArgv:
    def test_returns_list(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["ax", "gateway", "start"])
        # The function should return a list of strings
        argv = _gw_daemon._gateway_cli_argv("gateway", "run")
        assert isinstance(argv, list)
        assert "gateway" in argv
        assert "run" in argv

    def test_fallback_python_c(self, monkeypatch):
        monkeypatch.setattr("sys.argv", [""])
        monkeypatch.setattr("shutil.which", lambda name: None)
        argv = _gw_daemon._gateway_cli_argv("gateway", "run")
        assert isinstance(argv, list)
        # Should use python -c fallback
        assert any("-c" in a for a in argv) or any("ax" in a for a in argv)


class TestWaitForUiReady:
    def test_returns_true_when_port_open(self, monkeypatch):
        process = MagicMock()
        process.poll.return_value = None
        # Patch socket to connect immediately
        monkeypatch.setattr(
            socket,
            "create_connection",
            lambda addr, **kw: MagicMock(
                __enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)
            ),
        )
        assert _gw_daemon._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.5) is True

    def test_returns_false_when_process_dies(self, monkeypatch):
        process = MagicMock()
        process.poll.return_value = 1  # process exited

        def _fail(*a, **kw):
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", _fail)
        assert _gw_daemon._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.2) is False

    def test_returns_false_when_timeout(self, monkeypatch):
        process = MagicMock()
        process.poll.return_value = None

        def _fail(*a, **kw):
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", _fail)
        assert _gw_daemon._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.2) is False


class TestTerminatePids:
    def test_empty_list(self):
        requested, forced = _gw_daemon._terminate_pids([], timeout=0.1)
        assert requested == []
        assert forced == []

    def test_process_lookup_error_skipped(self, monkeypatch):
        def _kill(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", _kill)
        requested, forced = _gw_daemon._terminate_pids([99999], timeout=0.1)
        assert requested == []

    def test_process_terminates_normally(self, monkeypatch):
        killed = []

        def _kill(pid, sig):
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", _kill)
        monkeypatch.setattr(_gw_daemon.gateway_core, "_pid_alive", lambda pid: False)
        requested, forced = _gw_daemon._terminate_pids([1234], timeout=0.2)
        assert requested == [1234]
        assert forced == []

    def test_process_needs_sigkill(self, monkeypatch):
        killed = []

        def _kill(pid, sig):
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", _kill)
        monkeypatch.setattr(_gw_daemon.gateway_core, "_pid_alive", lambda pid: True)
        requested, forced = _gw_daemon._terminate_pids([1234], timeout=0.1)
        assert requested == [1234]
        assert forced == [1234]
        # Should have sent both SIGTERM and SIGKILL
        assert any(s == signal.SIGTERM for _, s in killed)
        assert any(s == signal.SIGKILL for _, s in killed)


class TestGatewayStopForcedKills:
    def test_forced_kill_output(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "active_gateway_pids", lambda: [9999])
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pids", lambda: [9998])
        monkeypatch.setattr(_gw_daemon, "_terminate_pids", lambda pids, **kw: (pids, pids))  # all forced
        monkeypatch.setattr(_gw_daemon, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(_gw_daemon.gateway_core, "clear_gateway_pid", lambda: None)
        monkeypatch.setattr(_gw_daemon, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "Forced" in output or "force" in output.lower()


class TestGatewayStartBranches:
    def test_daemon_fails_to_start(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 9999
        monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(_gw_daemon, "_wait_for_daemon_ready", lambda proc: False)
        monkeypatch.setattr(_gw_daemon, "_tail_log_lines", lambda path: "Error in daemon")
        monkeypatch.setattr(_gw_daemon, "daemon_log_path", lambda: Path("/tmp/gw.log"))

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code != 0
        assert "Failed" in _strip(result.output)

    def test_ui_fails_to_start(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: 1111)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 2222
        monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda proc, **kw: False)
        monkeypatch.setattr(_gw_daemon, "_tail_log_lines", lambda path: "Port in use")
        monkeypatch.setattr(_gw_daemon, "ui_log_path", lambda: Path("/tmp/ui.log"))
        monkeypatch.setattr(_gw_daemon, "daemon_log_path", lambda: Path("/tmp/gw.log"))

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code != 0
        assert "Failed" in _strip(result.output)

    def test_both_start_fresh(self, monkeypatch):
        monkeypatch.setattr(_gw_daemon, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 5000
        monkeypatch.setattr(_gw_daemon, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(_gw_daemon, "_wait_for_daemon_ready", lambda proc: True)

        # After daemon, active_gateway_pid should return a pid
        call_count = {"pid": 0}

        def _active_pid():
            call_count["pid"] += 1
            return 5000 if call_count["pid"] > 1 else None

        monkeypatch.setattr(_gw_daemon, "active_gateway_pid", _active_pid)
        monkeypatch.setattr(_gw_daemon, "_wait_for_ui_ready", lambda proc, **kw: True)

        ui_count = {"n": 0}

        def _active_ui_pid():
            ui_count["n"] += 1
            return 5001 if ui_count["n"] > 1 else None

        monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", _active_ui_pid)
        monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": True, "url": "http://localhost:8765"})
        monkeypatch.setattr(_gw_daemon, "daemon_log_path", lambda: Path("/tmp/gw.log"))
        monkeypatch.setattr(_gw_daemon, "ui_log_path", lambda: Path("/tmp/ui.log"))
        monkeypatch.setattr(_gw_daemon, "record_gateway_activity", lambda *a, **kw: None)

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "started" in output.lower()


def test_start_warns_when_offline_and_session_both_present(monkeypatch, tmp_path):
    """The silent-downgrade danger zone: real session + AX_OFFLINE=1.

    The operator gets a prominent foreground warning naming the session file
    path and the recovery action (`unset AX_OFFLINE`).
    """
    monkeypatch.setenv("AX_OFFLINE", "1")
    _seed_real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1 is set" in result.output
    assert "real gateway session" in result.output
    assert "unset AX_OFFLINE" in result.output


def test_start_no_warning_when_offline_only(monkeypatch, tmp_path):
    """Intentional offline use: AX_OFFLINE=1 with no real session file.

    No warning fires; this is the offline-development happy path PR #215
    optimized for.
    """
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config_empty"))
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1" not in result.output
    assert "unset AX_OFFLINE" not in result.output


def test_start_no_warning_when_session_only(monkeypatch, tmp_path):
    """Normal real-platform use: real session, no AX_OFFLINE.

    No warning fires; backward compatibility with the pre-#215 start flow.
    """
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    _seed_real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_daemon, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(_gw_daemon, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(_gw_daemon, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1" not in result.output


def test_online_run_clears_stale_offline_lock(monkeypatch, tmp_path):
    """Starting the daemon in online mode (no AX_OFFLINE) clears any stale
    lock from a previous offline run that was SIGKILL'd before its finally
    clause ran. Without this, `ax gateway status` would lie about the
    daemon's mode for the rest of the session.
    """
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))

    # Simulate a SIGKILL'd previous offline daemon: the lock exists on disk
    # but no daemon is actually running in offline mode.
    _gw_daemon._write_offline_mode_lock()
    assert _gw_ui._is_offline_mode_active(), "precondition: stale lock present"

    # Stub the heavy daemon-bring-up so the run command can exit cleanly
    # without spawning anything or requiring a real session.
    fake_session = {"token": "real", "base_url": "https://paxai.app", "space_id": "s1"}
    monkeypatch.setattr(_gw_daemon, "_load_gateway_session_or_exit", lambda: fake_session)
    fake_daemon = MagicMock()
    fake_daemon.run = MagicMock(return_value=None)
    monkeypatch.setattr(_gw_daemon, "GatewayDaemon", lambda **kw: fake_daemon)

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 0, result.output
    assert not _gw_ui._is_offline_mode_active(), "stale lock should be cleared at the top of the online run path"
    assert not _gw_ui._offline_mode_lock_path().exists()
