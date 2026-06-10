"""Per-module gateway command tests: gateway_lifecycle (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_lifecycle."""

from __future__ import annotations

import io
import json

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_lifecycle as _gw_lifecycle
from ax_cli.main import app
from tests.gateway_cmd_testlib import _isolate_gateway_paths, _strip

runner = CliRunner()


def test_gateway_agents_attach_writes_channel_config_and_command(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    workdir = tmp_path / "roger"
    token_file = tmp_path / "roger.token"
    token_file.write_text("axp_a_agent.secret\n")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "roger",
            "agent_id": "agent-roger",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "workdir": str(workdir),
            "desired_state": "stopped",
            "effective_state": "stale",
            "transport": "gateway",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    def fake_write_channel_setup(*, agent_name, workdir, **kwargs):
        return {
            "agent": agent_name,
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "mode": "local",
            "mcp_path": str(workdir / ".mcp.json"),
            "env_path": str(tmp_path / "roger.env"),
            "cli_config_path": str(workdir / ".ax" / "config.toml"),
            "cli_readme_path": str(workdir / ".ax" / "README.md"),
            "server_name": "ax-channel",
            "launch_command": f"claude --strict-mcp-config --mcp-config {workdir / '.mcp.json'} "
            "--dangerously-load-development-channels server:ax-channel",
        }

    from ax_cli.commands import channel as channel_mod

    monkeypatch.setattr(channel_mod, "write_channel_setup", fake_write_channel_setup)

    result = runner.invoke(app, ["gateway", "agents", "attach", "roger", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "roger"
    assert payload["attach_command"].startswith(f"cd {workdir}")
    assert "--dangerously-load-development-channels server:ax-channel" in payload["attach_command"]
    updated = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "roger")
    assert updated["desired_state"] == "running"


def test_gateway_agents_mark_attached_makes_manual_session_active(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "roger.token"
    token_file.write_text("axp_a_agent.secret\n")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "roger",
            "agent_id": "agent-roger",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "workdir": str(tmp_path / "roger"),
            "desired_state": "stopped",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "agents", "mark-attached", "roger", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["desired_state"] == "running"
    assert payload["effective_state"] == "running"
    assert payload["manual_attach_state"] == "attached"
    assert payload["local_attach_state"] == "manual_attached"
    assert payload["connected"] is True
    assert payload["presence"] == "IDLE"
    assert payload["reachability"] == "live_now"


def test_launch_attached_agent_session_uses_script_log_without_stdout_duplication(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    workdir = tmp_path / "roger"
    workdir.mkdir()
    mcp_path = workdir / ".mcp.json"
    mcp_path.write_text("{}")
    gateway_core.save_gateway_registry(
        {
            "agents": [
                {
                    "name": "roger",
                    "runtime_type": "claude_code_channel",
                    "template_id": "claude_code_channel",
                    "workdir": str(workdir),
                    "desired_state": "stopped",
                }
            ]
        }
    )

    which_calls = []

    def fake_which(name):
        which_calls.append(name)
        if name == "claude":
            return "/usr/local/bin/claude"
        if name == "script":
            return "/usr/bin/script"
        return None

    popen_calls = []

    class FakeProcess:
        pid = 9876

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        popen_calls.append({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(_gw_lifecycle.shutil, "which", fake_which)
    monkeypatch.setattr(_gw_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(_gw_lifecycle.subprocess, "Popen", fake_popen)

    payload = _gw_lifecycle._launch_attached_agent_session(
        {
            "agent": "roger",
            "mcp_path": str(mcp_path),
            "server_name": "ax-channel",
            "launch_command": "claude --strict-mcp-config --mcp-config .mcp.json",
        }
    )

    assert payload["launched"] is True
    assert which_calls == ["claude", "script"]
    assert popen_calls[0]["command"][:3] == [
        "/usr/bin/script",
        "-q",
        str(config_dir / "gateway" / "agents" / "roger" / "attached-session.log"),
    ]
    assert popen_calls[0]["stdout"] == _gw_lifecycle.subprocess.DEVNULL
    assert popen_calls[0]["stdin"] == _gw_lifecycle.subprocess.PIPE
    entry = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "roger")
    assert entry["attached_session_pid"] == 9876
    assert entry["effective_state"] == "starting"


def test_operator_start_clears_error_state(monkeypatch, tmp_path):
    """ax gateway agents start must clear all setup-error/disable fields."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
        }
    )

    registry = {
        "agents": [
            {
                "name": "errored-agent",
                "agent_id": "agent-err",
                "template_id": "echo",
                "runtime_type": "echo",
                "desired_state": "stopped",
                "setup_disabled": True,
                "setup_disabled_at": gateway_core._now_iso(),
                "setup_disabled_reason": "Auto-disabled after 10 errors",
                "consecutive_setup_errors": 10,
                "last_setup_error_signature": "some error",
                "last_runtime_error_at": gateway_core._now_iso(),
            }
        ],
    }
    gateway_core.save_gateway_registry(registry)

    _gw_lifecycle._set_managed_agent_desired_state("errored-agent", "running")
    reloaded = gateway_core.load_gateway_registry()
    entry = next(a for a in reloaded["agents"] if a["name"] == "errored-agent")
    assert entry["desired_state"] == "running"
    assert entry.get("setup_disabled") is False
    assert entry.get("consecutive_setup_errors") == 0
    assert entry.get("last_runtime_error_at") is None
    assert entry.get("setup_disabled_at") is None


def test_agents_start_refuses_when_daemon_stopped(monkeypatch, tmp_path):
    # #158 — without the Gateway daemon there is no supervisor to bring the
    # agent up; the previous behaviour returned exit 0 with a success message,
    # leaving the operator to discover effective_state=stopped via `show`.
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session({"token": "axp_u_test", "base_url": "https://paxai.app", "space_id": "space-1"})
    registry = {
        "agents": [
            {
                "name": "echo-demo",
                "agent_id": "agent-echo",
                "template_id": "echo_test",
                "runtime_type": "echo",
                "desired_state": "stopped",
            }
        ],
    }
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: None)

    result = runner.invoke(app, ["gateway", "agents", "start", "echo-demo"])

    assert result.exit_code == 1, result.output
    assert "Gateway daemon is stopped" in result.output
    assert "ax gateway start" in result.output
    reloaded = gateway_core.load_gateway_registry()
    entry = next(a for a in reloaded["agents"] if a["name"] == "echo-demo")
    assert entry["desired_state"] == "stopped"


def test_agents_start_proceeds_when_daemon_running(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session({"token": "axp_u_test", "base_url": "https://paxai.app", "space_id": "space-1"})
    registry = {
        "agents": [
            {
                "name": "echo-demo",
                "agent_id": "agent-echo",
                "template_id": "echo_test",
                "runtime_type": "echo",
                "desired_state": "stopped",
            }
        ],
    }
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: 12345)

    result = runner.invoke(app, ["gateway", "agents", "start", "echo-demo"])

    assert result.exit_code == 0, result.output
    assert "Desired state set to running" in result.output
    reloaded = gateway_core.load_gateway_registry()
    entry = next(a for a in reloaded["agents"] if a["name"] == "echo-demo")
    assert entry["desired_state"] == "running"


class TestAgentsStartStopCommand:
    def test_start(self, monkeypatch):
        monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: 12345)
        monkeypatch.setattr(_gw_lifecycle, "_set_managed_agent_desired_state", lambda name, state: {"ok": True})
        result = runner.invoke(app, ["gateway", "agents", "start", "bot1"])
        assert result.exit_code == 0
        assert "running" in _strip(result.output).lower()

    def test_start_not_found(self, monkeypatch):
        monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: 12345)
        monkeypatch.setattr(
            _gw_lifecycle,
            "_set_managed_agent_desired_state",
            lambda name, state: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "start", "nope"])
        assert result.exit_code != 0

    def test_stop(self, monkeypatch):
        monkeypatch.setattr(_gw_lifecycle, "_set_managed_agent_desired_state", lambda name, state: {"ok": True})
        result = runner.invoke(app, ["gateway", "agents", "stop", "bot1"])
        assert result.exit_code == 0
        assert "stopped" in _strip(result.output).lower()

    def test_stop_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_lifecycle,
            "_set_managed_agent_desired_state",
            lambda name, state: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "stop", "nope"])
        assert result.exit_code != 0


class TestAgentsMarkAttachedCommand:
    def test_json(self, monkeypatch):
        payload = {"name": "bot1", "state": "active"}
        monkeypatch.setattr(_gw_lifecycle, "_mark_attached_agent_session", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_lifecycle,
            "_mark_attached_agent_session",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "nope"])
        assert result.exit_code != 0


class TestAgentsAttachCommand:
    def test_json(self, monkeypatch):
        payload = {
            "mcp_path": "/tmp/w/.ax/mcp.json",
            "env_path": "/tmp/w/.ax/.env",
            "server_name": "ax-channel",
            "agent": "bot1",
            "attach_command": "cd /tmp/w && claude ...",
            "launch_command": "claude ...",
        }
        monkeypatch.setattr(_gw_lifecycle, "_prepare_attached_agent_payload", lambda name: payload)
        result = runner.invoke(app, ["gateway", "agents", "attach", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_lifecycle,
            "_prepare_attached_agent_payload",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "attach", "nope"])
        assert result.exit_code != 0

    def test_not_attached(self, monkeypatch):
        monkeypatch.setattr(
            _gw_lifecycle,
            "_prepare_attached_agent_payload",
            lambda name: (_ for _ in ()).throw(ValueError("not attached")),
        )
        result = runner.invoke(app, ["gateway", "agents", "attach", "nope"])
        assert result.exit_code != 0


class TestAgentsMarkAttachedTextRendering:
    def test_text_success(self, monkeypatch):
        monkeypatch.setattr(_gw_lifecycle, "_mark_attached_agent_session", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "manually attached" in output.lower()


class TestAgentsAttachTextRendering:
    def test_text_success(self, monkeypatch):
        payload = {
            "mcp_path": "/tmp/w/.ax/mcp.json",
            "env_path": "/tmp/w/.ax/.env",
            "server_name": "ax-channel",
            "agent": "bot1",
            "attach_command": "cd /tmp/w && claude ...",
            "launch_command": "claude ...",
        }
        monkeypatch.setattr(_gw_lifecycle, "_prepare_attached_agent_payload", lambda name: payload)
        result = runner.invoke(app, ["gateway", "agents", "attach", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "channel ready" in output.lower()
        assert "bot1" in output


# ── agents restart ────────────────────────────────────────────────────────


def test_agents_restart_refuses_when_daemon_stopped(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session({"token": "axp_u_test", "base_url": "https://paxai.app", "space_id": "space-1"})
    registry = {
        "agents": [
            {
                "name": "echo-demo",
                "agent_id": "agent-echo",
                "template_id": "echo_test",
                "runtime_type": "echo",
                "desired_state": "running",
            }
        ],
    }
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: None)

    result = runner.invoke(app, ["gateway", "agents", "restart", "echo-demo"])

    assert result.exit_code == 1, result.output
    assert "Gateway daemon is stopped" in result.output
    assert "ax gateway start" in result.output
    reloaded = gateway_core.load_gateway_registry()
    entry = next(a for a in reloaded["agents"] if a["name"] == "echo-demo")
    assert entry["desired_state"] == "running"


def test_agents_restart_stops_then_starts(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session({"token": "axp_u_test", "base_url": "https://paxai.app", "space_id": "space-1"})
    registry = {
        "agents": [
            {
                "name": "echo-demo",
                "agent_id": "agent-echo",
                "template_id": "echo_test",
                "runtime_type": "echo",
                "desired_state": "running",
            }
        ],
    }
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: 12345)

    result = runner.invoke(app, ["gateway", "agents", "restart", "echo-demo"])

    assert result.exit_code == 0, result.output
    assert "Desired state set to stopped" in result.output
    assert "Desired state set to running" in result.output
    reloaded = gateway_core.load_gateway_registry()
    entry = next(a for a in reloaded["agents"] if a["name"] == "echo-demo")
    assert entry["desired_state"] == "running"


def test_agents_restart_unknown_agent(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))

    gateway_core.save_gateway_session({"token": "axp_u_test", "base_url": "https://paxai.app", "space_id": "space-1"})
    gateway_core.save_gateway_registry({"agents": []})
    monkeypatch.setattr(_gw_lifecycle, "active_gateway_pid", lambda: 12345)

    result = runner.invoke(app, ["gateway", "agents", "restart", "no-such-agent"])

    assert result.exit_code == 1, result.output
    assert "Managed agent not found" in result.output
