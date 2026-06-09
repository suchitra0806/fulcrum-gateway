"""Per-module daemon command tests (gateway split #28 Phase 1 follow-up).

These are the rewritten counterparts of the daemon/process-management tests in
the skipped ``test_gateway_commands.py``. The behavior is identical; the only
change is that monkeypatches now target the **owning module**
(``ax_cli.commands.gateway_daemon_cmd``) where the ``start``/``stop``/``run``
commands resolve their helpers — instead of the pre-split
``ax_cli.commands.gateway`` namespace. ``gateway_core`` patches are unchanged
(``ax_cli.gateway`` was not split).

See docs/refactor/split-commands-gateway-removal.md.
"""

from __future__ import annotations

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_daemon_cmd as gw
from ax_cli.main import app

runner = CliRunner()


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


def _seed_session() -> None:
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )


def test_gateway_run_refuses_second_live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    _seed_session()
    gateway_core.write_gateway_pid(4242)
    monkeypatch.setattr(gateway_core, "_pid_alive", lambda pid: pid == 4242)

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 4242)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pid"] == 4242


def test_gateway_run_refuses_process_table_daemon_when_pid_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    _seed_session()
    monkeypatch.setattr(gateway_core, "_scan_gateway_process_pids", lambda: [5514])

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 5514)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pids"] == [5514]


def test_gateway_start_launches_background_daemon_and_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    _seed_session()

    state = {"daemon_pid": None, "ui_pid": None}
    spawned: list[tuple[list[str], str]] = []

    def fake_spawn(command, *, log_path):
        spawned.append((command, str(log_path)))
        if "run" in command:
            state["daemon_pid"] = 5514
            return _FakeProcess(5514)
        state["ui_pid"] = 5515
        return _FakeProcess(5515)

    monkeypatch.setattr(gw, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gw, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(gw, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(gw, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(gw, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        gw,
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
    assert len(spawned) == 2
    daemon_cmd, ui_cmd = spawned[0][0], spawned[1][0]
    assert "run" in daemon_cmd
    assert "ui" in ui_cmd


def test_gateway_start_without_login_starts_ui_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    state = {"ui_pid": None}
    spawned: list[list[str]] = []

    def fake_spawn(command, *, log_path):
        spawned.append(command)
        state["ui_pid"] = 6615
        return _FakeProcess(6615)

    monkeypatch.setattr(gw, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gw, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(gw, "active_gateway_pid", lambda: None)
    monkeypatch.setattr(gw, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        gw,
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
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(gw, "active_gateway_pids", lambda: [7714])
    monkeypatch.setattr(gw, "active_gateway_ui_pids", lambda: [7715])
    monkeypatch.setattr(
        gw,
        "_terminate_pids",
        lambda pids, timeout=3.0: (list(pids), [pids[0]] if pids and pids[0] == 7714 else []),
    )

    result = runner.invoke(app, ["gateway", "stop"])

    assert result.exit_code == 0, result.output
    assert "daemon = [7714]" in result.output
    assert "ui     = [7715]" in result.output
    assert "Forced kill:" in result.output


def test_gateway_start_rolls_back_daemon_when_ui_start_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    _seed_session()

    state = {"daemon_pid": None, "ui_pid": None}

    def fake_spawn(command, *, log_path):
        if "run" in command:
            state["daemon_pid"] = 8814
            return _FakeProcess(8814)
        state["ui_pid"] = 8815
        return _FakeProcess(8815)

    terminated: list[list[int]] = []
    cleared: list[int | None] = []

    monkeypatch.setattr(gw, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gw, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(gw, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: False)
    monkeypatch.setattr(gw, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(gw, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(gw, "_tail_log_lines", lambda path, lines=12: "address already in use")
    monkeypatch.setattr(
        gw, "_terminate_pids", lambda pids, timeout=3.0: terminated.append(list(pids)) or (list(pids), [])
    )
    monkeypatch.setattr(gateway_core, "clear_gateway_pid", lambda pid=None: cleared.append(pid))

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 1, result.output
    assert "Failed to start Gateway UI." in result.output
    assert terminated == [[8814]]
    assert cleared == [None]
