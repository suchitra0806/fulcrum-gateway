"""Extended CLI command tests for ax_cli/commands/gateway.py.

Covers the gateway CLI commands:
  status, start, stop, watch, run, login, activity,
  runtime-types, templates, runtime install, runtime status,
  spaces use, spaces current, spaces list,
  approvals list/show/approve/deny/cleanup,
  agents add/update/list/show/test/move/doctor/send/inbox/start/stop/
         attach/mark-attached/archive/restore/recover/remove,
  local connect/init/send/inbox,
  and internal helpers (_is_request_host_allowed, _tail_log_lines,
  _gateway_local_config_text, _gateway_local_config_from_workdir,
  _resolve_local_gateway_identity, _local_route_failure_guidance,
  _approval_required_guidance, _ensure_workdir,
  _print_pending_reply_warning_local, _check_local_pending_replies).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import typer
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway as gw_cmd
from ax_cli.main import app

runner = CliRunner()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return ANSI_RE.sub("", text)


# ── _is_request_host_allowed ──────────────────────────────────────────


class TestIsRequestHostAllowed:
    def test_localhost_allowed(self):
        assert gw_cmd._is_request_host_allowed("localhost") is True

    def test_localhost_with_port(self):
        assert gw_cmd._is_request_host_allowed("localhost:8765") is True

    def test_loopback_ip(self):
        assert gw_cmd._is_request_host_allowed("127.0.0.1") is True

    def test_loopback_ip_with_port(self):
        assert gw_cmd._is_request_host_allowed("127.0.0.1:9999") is True

    def test_external_host_rejected(self):
        assert gw_cmd._is_request_host_allowed("evil.com") is False

    def test_none_rejected(self):
        assert gw_cmd._is_request_host_allowed(None) is False

    def test_empty_rejected(self):
        assert gw_cmd._is_request_host_allowed("") is False

    def test_whitespace_only_rejected(self):
        assert gw_cmd._is_request_host_allowed("   ") is False

    def test_case_insensitive(self):
        assert gw_cmd._is_request_host_allowed("LOCALHOST") is True
        assert gw_cmd._is_request_host_allowed("LocalHost:8080") is True


# ── _tail_log_lines ──────────────────────────────────────────────────


class TestTailLogLines:
    def test_nonexistent_file(self, tmp_path):
        assert gw_cmd._tail_log_lines(tmp_path / "nope.log") == ""

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        assert gw_cmd._tail_log_lines(p) == ""

    def test_returns_last_lines(self, tmp_path):
        p = tmp_path / "test.log"
        p.write_text("\n".join(f"line-{i}" for i in range(20)))
        result = gw_cmd._tail_log_lines(p, lines=3)
        assert "line-17" in result
        assert "line-18" in result
        assert "line-19" in result
        assert "line-0" not in result

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "test.log"
        p.write_text("first\n\n\nsecond\n\n")
        result = gw_cmd._tail_log_lines(p, lines=5)
        assert "first" in result
        assert "second" in result


# ── _gateway_local_config_text ────────────────────────────────────────


class TestGatewayLocalConfigText:
    def test_basic_output(self):
        text = gw_cmd._gateway_local_config_text(
            agent_name="test-bot",
            gateway_url="http://127.0.0.1:8765",
        )
        assert 'mode = "local"' in text
        assert 'agent_name = "test-bot"' in text
        assert 'url = "http://127.0.0.1:8765"' in text

    def test_with_workdir(self):
        text = gw_cmd._gateway_local_config_text(
            agent_name="bot",
            gateway_url="http://localhost:8765",
            workdir="/tmp/w",
        )
        assert 'workdir = "/tmp/w"' in text

    def test_without_workdir(self):
        text = gw_cmd._gateway_local_config_text(
            agent_name="bot",
            gateway_url="http://localhost:8765",
        )
        assert "workdir" not in text


# ── _gateway_local_config_from_workdir ────────────────────────────────


class TestGatewayLocalConfigFromWorkdir:
    def test_none_workdir(self):
        assert gw_cmd._gateway_local_config_from_workdir(None) == {}

    def test_empty_workdir(self):
        assert gw_cmd._gateway_local_config_from_workdir("") == {}

    def test_missing_config(self, tmp_path):
        assert gw_cmd._gateway_local_config_from_workdir(str(tmp_path)) == {}

    def test_valid_config(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "mybot"\nregistry_ref = "ref-1"\n'
        )
        result = gw_cmd._gateway_local_config_from_workdir(str(tmp_path))
        assert result["agent_name"] == "mybot"
        assert result["registry_ref"] == "ref-1"
        assert result["gateway_url"] == "http://127.0.0.1:8765"

    def test_non_local_mode_without_url_returns_empty(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text('[agent]\nagent_name = "mybot"\n')
        result = gw_cmd._gateway_local_config_from_workdir(str(tmp_path))
        assert result == {}

    def test_invalid_toml(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text("not valid [[ toml {{{")
        result = gw_cmd._gateway_local_config_from_workdir(str(tmp_path))
        assert result == {}


# ── _resolve_local_gateway_identity ───────────────────────────────────


class TestResolveLocalGatewayIdentity:
    def test_agent_name_passthrough(self, tmp_path):
        name, ref = gw_cmd._resolve_local_gateway_identity(agent_name="bot", registry_ref=None, workdir=str(tmp_path))
        assert name == "bot"
        assert ref is None

    def test_registry_ref_passthrough(self, tmp_path):
        name, ref = gw_cmd._resolve_local_gateway_identity(agent_name=None, registry_ref="ref-1", workdir=str(tmp_path))
        assert name is None
        assert ref == "ref-1"

    def test_reads_from_workdir_config(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://localhost:8765"\n\n[agent]\nagent_name = "configured-bot"\n'
        )
        name, ref = gw_cmd._resolve_local_gateway_identity(agent_name=None, registry_ref=None, workdir=str(tmp_path))
        assert name == "configured-bot"

    def test_mismatch_raises(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://localhost:8765"\n\n[agent]\nagent_name = "configured-bot"\n'
        )
        with pytest.raises(ValueError, match="identity mismatch"):
            gw_cmd._resolve_local_gateway_identity(agent_name="different-bot", registry_ref=None, workdir=str(tmp_path))


# ── _local_route_failure_guidance ─────────────────────────────────────


class TestLocalRouteFailureGuidance:
    def test_404_includes_suggestions(self):
        msg = gw_cmd._local_route_failure_guidance(
            detail="not found",
            status_code=404,
            gateway_url="http://127.0.0.1:8765",
            agent_name="mybot",
            workdir="/tmp/w",
            action="local connect",
        )
        assert "Live Listener" in msg
        assert "ax gateway agents list --json" in msg
        assert "ax gateway local connect mybot" in msg

    def test_non_404_is_terse(self):
        msg = gw_cmd._local_route_failure_guidance(
            detail="server error",
            status_code=500,
            gateway_url="http://127.0.0.1:8765",
            agent_name="mybot",
            workdir="/tmp/w",
            action="local connect",
        )
        assert "Live Listener" not in msg
        assert "open http://127.0.0.1:8765" in msg

    def test_no_agent_name(self):
        msg = gw_cmd._local_route_failure_guidance(
            detail="err",
            status_code=None,
            gateway_url="http://127.0.0.1:8765",
            agent_name=None,
            workdir=None,
            action="proxy",
        )
        assert "this workspace" in msg


# ── _approval_required_guidance ───────────────────────────────────────


class TestApprovalRequiredGuidance:
    def test_basic_guidance(self):
        msg = gw_cmd._approval_required_guidance(
            connect_payload={
                "status": "pending",
                "approval_id": "appr-1",
                "agent": {"name": "mybot", "space_id": "sp-1"},
            },
            gateway_url="http://127.0.0.1:8765",
            agent_name="mybot",
            workdir="/tmp/w",
            action="send or poll",
        )
        assert "approval required" in msg.lower()
        assert "appr-1" in msg
        assert "mybot" in msg
        assert "Do not fall back to a direct PAT" in msg

    def test_no_approval_id(self):
        msg = gw_cmd._approval_required_guidance(
            connect_payload={"status": "pending"},
            gateway_url="http://127.0.0.1:8765",
        )
        assert "approval required" in msg.lower()


# ── _ensure_workdir ──────────────────────────────────────────────────


class TestEnsureWorkdir:
    def test_existing_dir_ok(self, tmp_path):
        gw_cmd._ensure_workdir(tmp_path, create=False)

    def test_file_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        with pytest.raises(typer.BadParameter, match="not a directory"):
            gw_cmd._ensure_workdir(f, create=False)

    def test_missing_no_create_raises(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(typer.BadParameter, match="does not exist"):
            gw_cmd._ensure_workdir(missing, create=False)

    def test_create_makes_dir(self, tmp_path):
        target = tmp_path / "new" / "deep"
        gw_cmd._ensure_workdir(target, create=True)
        assert target.is_dir()


# ── _print_pending_reply_warning_local ────────────────────────────────


class TestPrintPendingReplyWarningLocal:
    def test_no_warning_on_zero(self, capsys):
        gw_cmd._print_pending_reply_warning_local({"count": 0})
        # nothing printed to stdout
        assert capsys.readouterr().out == ""

    def test_no_warning_on_non_dict(self, capsys):
        gw_cmd._print_pending_reply_warning_local("not a dict")
        assert capsys.readouterr().out == ""


# ── _check_local_pending_replies ──────────────────────────────────────


class TestCheckLocalPendingReplies:
    def test_returns_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_poll_local_inbox_over_http", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        result = gw_cmd._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_returns_empty_on_non_dict(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_poll_local_inbox_over_http", lambda **kw: "not-dict")
        result = gw_cmd._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_returns_empty_when_no_messages(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_poll_local_inbox_over_http", lambda **kw: {"messages": []})
        result = gw_cmd._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_extracts_senders_and_ids(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {
                "messages": [
                    {"id": "m1", "display_name": "alice"},
                    {"id": "m2", "agent_name": "bob"},
                ],
                "unread_count": 2,
            },
        )
        result = gw_cmd._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 2
        assert result["message_ids"] == ["m1", "m2"]
        assert result["newest_senders"] == ["alice", "bob"]


# ── CLI: gateway status ──────────────────────────────────────────────


class TestStatusCommand:
    def test_status_json(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 123},
            "ui": {"running": True, "pid": 456, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "testuser",
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
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["connected"] is True

    def test_status_text(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": False,
            "daemon": {"running": False, "pid": None},
            "ui": {"running": False, "pid": None, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
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
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "gateway_dir" in output


# ── CLI: gateway activity ────────────────────────────────────────────


class TestActivityCommand:
    def test_activity_json_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: tmp_path / "activity.jsonl")
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []

    def test_activity_json_with_data(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1"},
            {"ts": "2026-01-01T00:01:00", "event": "test2", "agent_name": "bot2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_activity_filter_agent(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1"},
            {"ts": "2026-01-01T00:01:00", "event": "test2", "agent_name": "bot2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--agent", "bot1", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["events"][0]["agent_name"] == "bot1"

    def test_activity_filter_message_id(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "send", "message_id": "msg-1"},
            {"ts": "2026-01-01T00:01:00", "event": "recv", "message_id": "msg-2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--message-id", "msg-1", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["message_id"] == "msg-1"

    def test_activity_limit(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [{"ts": f"2026-01-01T00:0{i}:00", "event": f"ev{i}"} for i in range(5)]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--limit", "2", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_activity_text_no_data(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: tmp_path / "nope.jsonl")
        result = runner.invoke(app, ["gateway", "activity"])
        assert result.exit_code == 0
        assert "No Gateway activity" in _strip(result.output)


# ── CLI: gateway runtime-types ────────────────────────────────────────


class TestRuntimeTypesCommand:
    def test_json(self, monkeypatch):
        payload = {
            "runtime_types": [
                {"id": "echo", "label": "Echo", "kind": "builtin", "signals": {"activity": "yes", "tools": "no"}},
            ]
        }
        monkeypatch.setattr(gw_cmd, "_runtime_types_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "runtime-types", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["runtime_types"][0]["id"] == "echo"

    def test_text(self, monkeypatch):
        payload = {
            "runtime_types": [
                {"id": "echo", "label": "Echo", "kind": "builtin", "signals": {"activity": "yes", "tools": "no"}},
            ]
        }
        monkeypatch.setattr(gw_cmd, "_runtime_types_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "runtime-types"])
        assert result.exit_code == 0


# ── CLI: gateway templates ────────────────────────────────────────────


class TestTemplatesCommand:
    def test_json(self, monkeypatch):
        payload = {
            "templates": [
                {
                    "id": "echo_test",
                    "label": "Echo Test",
                    "asset_type_label": "test",
                    "output_label": "echo",
                    "availability": "ga",
                    "operator_summary": "Just echoes",
                    "signals": {"activity": "yes"},
                },
            ]
        }
        monkeypatch.setattr(gw_cmd, "_agent_templates_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "templates", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["templates"][0]["id"] == "echo_test"

    def test_text(self, monkeypatch):
        payload = {
            "templates": [
                {
                    "id": "echo_test",
                    "label": "Echo Test",
                    "asset_type_label": "test",
                    "output_label": "echo",
                    "availability": "ga",
                    "operator_summary": "Just echoes",
                    "signals": {"activity": "yes"},
                },
            ]
        }
        monkeypatch.setattr(gw_cmd, "_agent_templates_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "templates"])
        assert result.exit_code == 0


# ── CLI: gateway runtime install ──────────────────────────────────────


class TestRuntimeInstallCommand:
    def test_no_session(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: None)
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0
        assert "login" in _strip(result.output).lower()

    def test_install_json(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            gw_cmd,
            "_install_runtime_payload",
            lambda tid, **kw: {"target": "/home/hermes", "steps": [], "ready": True},
        )
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ready"] is True

    def test_install_not_ready_exits_1(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            gw_cmd,
            "_install_runtime_payload",
            lambda tid, **kw: {"target": "/home/hermes", "steps": [], "ready": False},
        )
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0

    def test_install_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(**kw):
            raise ValueError("bad template")

        monkeypatch.setattr(gw_cmd, "_install_runtime_payload", lambda tid, **kw: _raise(**kw))
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0


# ── CLI: gateway runtime status ───────────────────────────────────────


class TestRuntimeStatusCommand:
    def test_unknown_template(self, monkeypatch):
        result = runner.invoke(app, ["gateway", "runtime", "status", "unknown_thing"])
        assert result.exit_code != 0
        assert "unknown" in _strip(result.output).lower()

    def test_hermes_ready_json(self, monkeypatch):
        import ax_cli.gateway as gw_core

        monkeypatch.setattr(
            gw_core, "hermes_setup_status", lambda entry: {"ready": True, "resolved_path": "/opt/hermes"}
        )
        result = runner.invoke(app, ["gateway", "runtime", "status", "hermes", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ready"] is True

    def test_hermes_not_ready(self, monkeypatch):
        import ax_cli.gateway as gw_core

        monkeypatch.setattr(
            gw_core,
            "hermes_setup_status",
            lambda entry: {"ready": False, "expected_path": "/home/hermes", "summary": "not installed"},
        )
        result = runner.invoke(app, ["gateway", "runtime", "status", "hermes"])
        assert result.exit_code != 0
        assert "not ready" in _strip(result.output).lower()


# ── CLI: gateway spaces current ───────────────────────────────────────


class TestSpacesCurrentCommand:
    def test_json(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "space_name": "Test", "base_url": "https://paxai.app", "username": "u"},
        )
        result = runner.invoke(app, ["gateway", "spaces", "current", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["space_id"] == "sp-1"

    def test_text(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "space_name": "Test", "base_url": "https://paxai.app", "username": "u"},
        )
        result = runner.invoke(app, ["gateway", "spaces", "current"])
        assert result.exit_code == 0


# ── CLI: gateway spaces list ─────────────────────────────────────────


class TestSpacesListCommand:
    def test_json(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
        }
        monkeypatch.setattr(gw_cmd, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["spaces"]) == 1

    def test_text_with_spaces(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
        }
        monkeypatch.setattr(gw_cmd, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0

    def test_text_no_spaces(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_spaces_payload", lambda: {"spaces": [], "active_space_id": None})
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0
        assert "No spaces" in _strip(result.output)


# ── CLI: gateway approvals list ───────────────────────────────────────


class TestApprovalsListCommand:
    def test_json(self, monkeypatch):
        payload = {"approvals": [], "count": 0, "pending": 0}
        monkeypatch.setattr(gw_cmd, "_approval_rows_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "approvals", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 0

    def test_text_empty(self, monkeypatch):
        payload = {"approvals": [], "count": 0, "pending": 0}
        monkeypatch.setattr(gw_cmd, "_approval_rows_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "approvals", "list"])
        assert result.exit_code == 0
        assert "No Gateway approvals" in _strip(result.output)


# ── CLI: gateway approvals show ───────────────────────────────────────


class TestApprovalsShowCommand:
    def test_json(self, monkeypatch):
        payload = {
            "approval": {
                "approval_id": "a-1",
                "asset_id": "asset-1",
                "gateway_id": "gw-1",
                "install_id": "inst-1",
                "approval_kind": "binding",
                "status": "pending",
                "risk": "low",
                "action": "bind",
                "resource": "/tmp",
                "reason": "test",
                "requested_at": "2026-01-01",
                "decided_at": None,
                "decision_scope": None,
                "candidate_binding": None,
            }
        }
        monkeypatch.setattr(gw_cmd, "_approval_detail_payload", lambda aid: payload)
        result = runner.invoke(app, ["gateway", "approvals", "show", "a-1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["approval"]["approval_id"] == "a-1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_approval_detail_payload",
            lambda aid: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "show", "bogus"])
        assert result.exit_code != 0


# ── CLI: gateway approvals approve ────────────────────────────────────


class TestApprovalsApproveCommand:
    def test_json(self, monkeypatch):
        payload = {"approval": {"approval_id": "a-1", "asset_id": "x", "decision_scope": "asset"}}
        monkeypatch.setattr(gw_cmd, "approve_gateway_approval", lambda aid, scope="asset": payload)
        result = runner.invoke(app, ["gateway", "approvals", "approve", "a-1", "--json"])
        assert result.exit_code == 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "approve_gateway_approval",
            lambda aid, scope="asset": (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "approve", "bogus"])
        assert result.exit_code != 0


# ── CLI: gateway approvals deny ──────────────────────────────────────


class TestApprovalsDenyCommand:
    def test_json(self, monkeypatch):
        payload = {"approval_id": "a-1", "asset_id": "x"}
        monkeypatch.setattr(gw_cmd, "deny_gateway_approval", lambda aid: payload)
        result = runner.invoke(app, ["gateway", "approvals", "deny", "a-1", "--json"])
        assert result.exit_code == 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "deny_gateway_approval",
            lambda aid: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "deny", "bogus"])
        assert result.exit_code != 0


# ── CLI: gateway approvals cleanup ────────────────────────────────────


class TestApprovalsCleanupCommand:
    def test_json(self, monkeypatch):
        payload = {"archived_count": 2, "remaining_pending": 1}
        monkeypatch.setattr(gw_cmd, "archive_stale_gateway_approvals", lambda: payload)
        result = runner.invoke(app, ["gateway", "approvals", "cleanup", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["archived_count"] == 2

    def test_text(self, monkeypatch):
        payload = {"archived_count": 0, "remaining_pending": 0}
        monkeypatch.setattr(gw_cmd, "archive_stale_gateway_approvals", lambda: payload)
        result = runner.invoke(app, ["gateway", "approvals", "cleanup"])
        assert result.exit_code == 0
        assert "Archived" in _strip(result.output)


# ── CLI: gateway agents add ──────────────────────────────────────────


class TestAgentsAddCommand:
    def test_json(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": None,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(gw_cmd, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "echo1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "echo1"

    def test_text(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": 30,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(gw_cmd, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "echo1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "echo1" in output

    def test_error_exits_1(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_register_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad agent")),
        )
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "bad"])
        assert result.exit_code != 0


# ── CLI: gateway agents update ────────────────────────────────────────


class TestAgentsUpdateCommand:
    def test_json(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": None,
        }
        monkeypatch.setattr(gw_cmd, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(app, ["gateway", "agents", "update", "echo1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "echo1"

    def test_error_exits_1(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_update_managed_agent",
            lambda **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "update", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents list ─────────────────────────────────────────


class TestAgentsListCommand:
    def test_json(self, monkeypatch):
        payload = {
            "agents": [{"name": "bot1", "runtime_type": "echo", "template_id": "echo_test"}],
            "summary": {
                "managed_agents": 1,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
            },
        }
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


# ── CLI: gateway agents show ─────────────────────────────────────────


class TestAgentsShowCommand:
    def test_json(self, monkeypatch):
        detail = {"agent": {"name": "bot1"}, "recent_activity": []}
        monkeypatch.setattr(gw_cmd, "_agent_detail_payload", lambda name, **kw: detail)
        result = runner.invoke(app, ["gateway", "agents", "show", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"]["name"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_agent_detail_payload", lambda name, **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "show", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents test ─────────────────────────────────────────


class TestAgentsTestCommand:
    def test_json(self, monkeypatch):
        payload = {
            "target_agent": "bot1",
            "recommended_prompt": "test message",
            "message": {"id": "msg-1"},
        }
        monkeypatch.setattr(gw_cmd, "_send_gateway_test_to_managed_agent", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "test", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["target_agent"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_send_gateway_test_to_managed_agent",
            lambda name, **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        result = runner.invoke(app, ["gateway", "agents", "test", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents move ─────────────────────────────────────────


class TestAgentsMoveCommand:
    def test_json(self, monkeypatch):
        payload = {"active_space_id": "sp-2", "active_space_name": "New Space", "previous_space_id": "sp-1"}
        monkeypatch.setattr(gw_cmd, "_move_managed_agent_space", lambda name, sid, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["active_space_id"] == "sp-2"

    def test_no_space_no_revert(self, monkeypatch):
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1"])
        assert result.exit_code != 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_move_managed_agent_space",
            lambda name, sid, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2"])
        assert result.exit_code != 0


# ── CLI: gateway agents doctor ────────────────────────────────────────


class TestAgentsDoctorCommand:
    def test_json(self, monkeypatch):
        payload = {
            "status": "passed",
            "summary": "all ok",
            "checks": [{"name": "connectivity", "status": "passed", "detail": "ok"}],
        }
        monkeypatch.setattr(gw_cmd, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "passed"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_run_gateway_doctor",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "doctor", "nope"])
        assert result.exit_code != 0


# ── channel_sse doctor check ─────────────────────────────────────────


class TestChannelSseDoctorCheck:
    """_run_gateway_doctor emits channel_sse check for claude_code_channel agents."""

    def _make_channel_entry(self, tmp_path, *, sse_connected=None, connected=True):
        token_file = tmp_path / "token"
        token_file.write_text("axp_a_agent.secret")
        entry = {
            "name": "claude-channel",
            "agent_id": "agent-cc",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": gateway_core._now_iso(),
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
        if sse_connected is not None:
            entry["sse_connected"] = sse_connected
        if connected:
            entry["connected"] = True
        return entry

    def test_channel_sse_passed_when_connected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=True, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = gw_cmd._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert "channel_sse" in check_names
        assert check_names["channel_sse"]["status"] == "passed"

    def test_channel_sse_failed_when_disconnected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=False, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = gw_cmd._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert "channel_sse" in check_names
        assert check_names["channel_sse"]["status"] == "failed"
        assert "SSE subscription is down" in check_names["channel_sse"]["detail"]

    def test_claude_code_session_passed_even_when_sse_disconnected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=False, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = gw_cmd._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert check_names.get("claude_code_session", {}).get("status") == "passed"


class TestTokenFileResolutionInRemoveAndDoctor:
    """#89 / #147: the `agents remove` and `agents doctor` flows resolve
    `token_file` through `resolve_agent_token_file`, so they work for both the
    new relative form and legacy absolute paths. The relative-form doctor case
    also pins the regression sarob flagged on PR #108 (resolving the relative
    path against CWD instead of gateway_dir reported a false agent_token
    failure)."""

    def _entry(self, name, token_file):
        return {
            "name": name,
            "agent_id": f"agent-{name}",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes",
            "template_id": "hermes",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": gateway_core._now_iso(),
            "token_file": token_file,
            "transport": "gateway",
            "credential_source": "gateway",
        }

    def _save(self, entry):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [entry]
        gateway_core.save_gateway_registry(registry)

    def test_doctor_agent_token_passes_for_relative_token_file(self):
        # The relative form resolves under gateway_dir(), not CWD (PR #108 bug).
        token_path = gateway_core.agent_token_path("nova")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", "agents/nova/token"))

        result = gw_cmd._run_gateway_doctor("nova")
        checks = {c["name"]: c for c in result["checks"]}
        assert checks["agent_token"]["status"] == "passed"

    def test_doctor_agent_token_passes_for_legacy_absolute_token_file(self, tmp_path):
        # A legacy absolute path (non-canonical shape, so the load-time
        # migration leaves it) is honored as-is by the resolver.
        legacy = tmp_path / "legacy_tokens" / "nova.token"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", str(legacy)))

        result = gw_cmd._run_gateway_doctor("nova")
        checks = {c["name"]: c for c in result["checks"]}
        assert checks["agent_token"]["status"] == "passed"

    def test_remove_unlinks_relative_token_file(self):
        token_path = gateway_core.agent_token_path("nova")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", "agents/nova/token"))

        gw_cmd._remove_managed_agent("nova", client_factory=lambda: None)
        assert not token_path.exists()

    def test_remove_unlinks_legacy_absolute_token_file(self, tmp_path):
        legacy = tmp_path / "legacy_tokens" / "nova.token"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", str(legacy)))

        gw_cmd._remove_managed_agent("nova", client_factory=lambda: None)
        assert not legacy.exists()


# ── CLI: gateway agents send ─────────────────────────────────────────


class TestAgentsSendCommand:
    def test_json(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "message": {"id": "msg-1"},
            "content": "hello",
        }
        monkeypatch.setattr(gw_cmd, "_send_from_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_send_from_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello"])
        assert result.exit_code != 0


# ── CLI: gateway agents inbox ────────────────────────────────────────


class TestAgentsInboxCommand:
    def test_json(self, monkeypatch):
        payload = {"agent": "bot1", "messages": [], "unread_count": 0}
        monkeypatch.setattr(gw_cmd, "_inbox_for_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "inbox", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_inbox_for_managed_agent",
            lambda **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "inbox", "nope"])
        assert result.exit_code != 0

    def test_value_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_inbox_for_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad param")),
        )
        result = runner.invoke(app, ["gateway", "agents", "inbox", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents start/stop ────────────────────────────────────


class TestAgentsStartStopCommand:
    def test_start(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_set_managed_agent_desired_state", lambda name, state: {"ok": True})
        result = runner.invoke(app, ["gateway", "agents", "start", "bot1"])
        assert result.exit_code == 0
        assert "running" in _strip(result.output).lower()

    def test_start_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_set_managed_agent_desired_state",
            lambda name, state: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "start", "nope"])
        assert result.exit_code != 0

    def test_stop(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_set_managed_agent_desired_state", lambda name, state: {"ok": True})
        result = runner.invoke(app, ["gateway", "agents", "stop", "bot1"])
        assert result.exit_code == 0
        assert "stopped" in _strip(result.output).lower()

    def test_stop_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_set_managed_agent_desired_state",
            lambda name, state: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "stop", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents mark-attached ─────────────────────────────────


class TestAgentsMarkAttachedCommand:
    def test_json(self, monkeypatch):
        payload = {"name": "bot1", "state": "active"}
        monkeypatch.setattr(gw_cmd, "_mark_attached_agent_session", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_mark_attached_agent_session",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway agents archive ───────────────────────────────────────


class TestAgentsArchiveCommand:
    def test_json_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_archive_managed_agent",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "archive", "nope", "--json"])
        assert result.exit_code != 0

    def test_text_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1"])
        assert result.exit_code == 0
        assert "Archived" in _strip(result.output)


# ── CLI: gateway agents restore ──────────────────────────────────────


class TestAgentsRestoreCommand:
    def test_json_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_restore_managed_agent", lambda name: {"name": name, "desired_state": "stopped"})
        result = runner.invoke(app, ["gateway", "agents", "restore", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_restore_managed_agent",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "restore", "nope", "--json"])
        assert result.exit_code != 0


# ── CLI: gateway agents recover ──────────────────────────────────────


class TestAgentsRecoverCommand:
    def test_json_success(self, monkeypatch):
        payload = {
            "recovered": [{"name": "bot1", "agent_id": "a1"}],
            "already_present": [],
            "no_evidence": [],
            "count": 1,
        }
        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_no_evidence(self, monkeypatch):
        payload = {"recovered": [], "already_present": [], "no_evidence": ["bot1"], "count": 0}
        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1", "--json"])
        assert result.exit_code != 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_recover_managed_agents_from_evidence",
            lambda names: (_ for _ in ()).throw(ValueError("broken")),
        )
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code != 0


# ── CLI: gateway agents remove ────────────────────────────────────────


class TestAgentsRemoveCommand:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_remove_managed_agent", lambda name: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "remove", "bot1"])
        assert result.exit_code == 0
        assert "Removed" in _strip(result.output)

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_remove_managed_agent",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "remove", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway stop ────────────────────────────────────────────────


class TestGatewayStopCommand:
    def test_already_stopped(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "active_gateway_pids", lambda: [])
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pids", lambda: [])
        monkeypatch.setattr(gw_cmd, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(gw_cmd.gateway_core, "clear_gateway_pid", lambda: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0
        assert "already stopped" in _strip(result.output).lower()

    def test_stops_processes(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "active_gateway_pids", lambda: [9999])
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pids", lambda: [9998])
        monkeypatch.setattr(gw_cmd, "_terminate_pids", lambda pids, **kw: (pids, []))
        monkeypatch.setattr(gw_cmd, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(gw_cmd.gateway_core, "clear_gateway_pid", lambda: None)
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0


# ── CLI: gateway start ───────────────────────────────────────────────


class TestGatewayStartCommand:
    def test_no_session_daemon_not_started_ui_starts(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: None)
        monkeypatch.setattr(gw_cmd, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 9999
        fake_proc.poll.return_value = None
        monkeypatch.setattr(gw_cmd, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(gw_cmd, "_wait_for_ui_ready", lambda proc, **kw: True)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: 9999)
        monkeypatch.setattr(gw_cmd, "ui_status", lambda: {"running": False, "url": None})
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0
        assert "not logged in" in _strip(result.output).lower() or "not started" in _strip(result.output).lower()

    def test_ui_already_running(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(gw_cmd, "active_gateway_pid", lambda: 1111)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: 2222)
        monkeypatch.setattr(gw_cmd, "ui_status", lambda: {"running": True, "url": "http://localhost:8765"})
        monkeypatch.setattr(gw_cmd, "daemon_log_path", lambda: Path("/tmp/gw.log"))
        monkeypatch.setattr(gw_cmd, "ui_log_path", lambda: Path("/tmp/ui.log"))
        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0


# ── CLI: gateway watch ───────────────────────────────────────────────


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
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        monkeypatch.setattr(gw_cmd, "_render_gateway_dashboard", lambda p: "dashboard")
        result = runner.invoke(app, ["gateway", "watch", "--once"])
        assert result.exit_code == 0


# ── CLI: gateway run ─────────────────────────────────────────────────


class TestGatewayRunCommand:
    def test_no_session(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: None)
        result = runner.invoke(app, ["gateway", "run"])
        assert result.exit_code != 0

    def test_runtime_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_load_gateway_session_or_exit", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(gw_cmd, "gateway_dir", lambda: Path("/tmp/gw"))

        class FakeDaemon:
            def __init__(self, **kw):
                pass

            def run(self, once=False):
                raise RuntimeError("lock held")

            def stop(self):
                pass

        monkeypatch.setattr(gw_cmd, "GatewayDaemon", FakeDaemon)
        result = runner.invoke(app, ["gateway", "run"])
        assert result.exit_code != 0


# ── CLI: gateway agents attach ────────────────────────────────────────


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
        monkeypatch.setattr(gw_cmd, "_prepare_attached_agent_payload", lambda name: payload)
        result = runner.invoke(app, ["gateway", "agents", "attach", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_prepare_attached_agent_payload",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "attach", "nope"])
        assert result.exit_code != 0

    def test_not_attached(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_prepare_attached_agent_payload",
            lambda name: (_ for _ in ()).throw(ValueError("not attached")),
        )
        result = runner.invoke(app, ["gateway", "agents", "attach", "nope"])
        assert result.exit_code != 0


# ── CLI: gateway local connect ────────────────────────────────────────


class TestLocalConnectCommand:
    def test_json(self, monkeypatch):
        payload = {"status": "approved", "session_token": "tok", "agent": {"name": "bot1"}}
        monkeypatch.setattr(gw_cmd, "_request_local_connect", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "local", "connect", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "approved"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_request_local_connect",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad")),
        )
        result = runner.invoke(app, ["gateway", "local", "connect", "bot1"])
        assert result.exit_code != 0


# ── CLI: gateway local send ──────────────────────────────────────────


class TestLocalSendCommand:
    def test_json_with_session(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", None),
        )
        monkeypatch.setattr(
            gw_cmd,
            "_check_local_pending_replies",
            lambda **kw: {"count": 0, "message_ids": [], "newest_senders": []},
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 201
        fake_resp.json.return_value = {"agent": "bot1", "message": {"id": "m1"}}
        fake_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: fake_resp)
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {"messages": []},
        )
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello world", "--session-token", "tok-123", "--json", "--no-inbox"],
        )
        assert result.exit_code == 0

    def test_send_error(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_resolve_local_gateway_session",
            lambda **kw: (_ for _ in ()).throw(ValueError("no session")),
        )
        result = runner.invoke(app, ["gateway", "local", "send", "hello", "--session-token", "bad"])
        assert result.exit_code != 0


# ── CLI: gateway local inbox ─────────────────────────────────────────


class TestLocalInboxCommand:
    def test_json_with_session(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok", None),
        )
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {"agent": "bot1", "messages": []},
        )
        result = runner.invoke(
            app,
            ["gateway", "local", "inbox", "--session-token", "tok", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["messages"] == []


# ── _gateway_cli_argv ─────────────────────────────────────────────────


class TestGatewayCliArgv:
    def test_returns_list(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["ax", "gateway", "start"])
        # The function should return a list of strings
        argv = gw_cmd._gateway_cli_argv("gateway", "run")
        assert isinstance(argv, list)
        assert "gateway" in argv
        assert "run" in argv

    def test_fallback_python_c(self, monkeypatch):
        monkeypatch.setattr("sys.argv", [""])
        monkeypatch.setattr("shutil.which", lambda name: None)
        argv = gw_cmd._gateway_cli_argv("gateway", "run")
        assert isinstance(argv, list)
        # Should use python -c fallback
        assert any("-c" in a for a in argv) or any("ax" in a for a in argv)


# ── _resolve_local_gateway_session ────────────────────────────────────


class TestResolveLocalGatewaySession:
    def test_returns_existing_token(self):
        token, payload = gw_cmd._resolve_local_gateway_session(session_token="my-token")
        assert token == "my-token"
        assert payload is None

    def test_connects_when_no_token(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_request_local_connect",
            lambda **kw: {"session_token": "new-tok", "status": "approved"},
        )
        token, payload = gw_cmd._resolve_local_gateway_session(session_token=None, agent_name="bot1")
        assert token == "new-tok"
        assert payload["status"] == "approved"

    def test_raises_on_pending(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_request_local_connect",
            lambda **kw: {"status": "pending"},
        )
        with pytest.raises(ValueError, match="approval required"):
            gw_cmd._resolve_local_gateway_session(session_token=None, agent_name="bot1")

    def test_raises_on_rejected(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_request_local_connect",
            lambda **kw: {"status": "rejected"},
        )
        with pytest.raises(ValueError, match="rejected"):
            gw_cmd._resolve_local_gateway_session(session_token=None, agent_name="bot1")


# ── CLI: gateway status text with agents and activity ─────────────────


class TestStatusTextRendering:
    def test_status_with_agents_and_alerts(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 100},
            "ui": {"running": True, "pid": 200, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "tester",
            "agents": [
                {
                    "name": "bot1",
                    "runtime_type": "echo",
                    "template_id": "echo_test",
                    "mode": "live",
                    "presence": "IDLE",
                    "output": "text",
                    "confidence": "HIGH",
                    "acting_agent_name": "bot1",
                    "active_space_name": "Test",
                    "last_seen_age_seconds": 5,
                    "backlog_depth": 0,
                    "confidence_reason": "ok",
                },
            ],
            "recent_activity": [
                {"ts": "2026-01-01", "event": "test", "agent_name": "bot1", "message_id": "m1", "reply_preview": "ok"},
            ],
            "summary": {
                "managed_agents": 1,
                "live_agents": 1,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 1,
                "system_agents": 1,
                "alert_count": 1,
                "pending_approvals": 0,
            },
            "alerts": [
                {"severity": "warn", "title": "test alert", "agent_name": "bot1", "detail": "check it"},
            ],
        }
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status"])
        assert result.exit_code == 0

    def test_status_with_all_flag(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 100},
            "ui": {"running": True, "pid": 200, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "tester",
            "agents": [],
            "recent_activity": [],
            "summary": {
                "managed_agents": 0,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 2,
                "system_agents": 1,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status", "--all"])
        assert result.exit_code == 0


# ── CLI: gateway agents send text rendering ───────────────────────────


class TestAgentsSendTextRendering:
    def test_text_output_with_inbox(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "message": {"id": "msg-1"},
            "content": "hello",
            "inbox": {
                "unread_count": 1,
                "messages": [
                    {"agent_name": "other", "content": "reply text"},
                ],
            },
        }
        monkeypatch.setattr(gw_cmd, "_send_from_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output

    def test_text_output_with_inbox_error(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "message": {"id": "msg-1"},
            "content": "hello",
            "inbox_error": "connection refused",
        }
        monkeypatch.setattr(gw_cmd, "_send_from_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello"])
        assert result.exit_code == 0


# ── CLI: gateway agents inbox text rendering ──────────────────────────


class TestAgentsInboxTextRendering:
    def test_text_with_messages(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "messages": [
                {"created_at": "2026-01-01", "display_name": "alice", "content": "hey"},
            ],
            "unread_count": 1,
        }
        monkeypatch.setattr(gw_cmd, "_inbox_for_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "inbox", "bot1"])
        assert result.exit_code == 0


# ── CLI: gateway agents test text rendering ───────────────────────────


class TestAgentsTestTextRendering:
    def test_text_output(self, monkeypatch):
        payload = {
            "target_agent": "bot1",
            "recommended_prompt": "test prompt",
            "message": {"id": "msg-1"},
        }
        monkeypatch.setattr(gw_cmd, "_send_gateway_test_to_managed_agent", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "test", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output
        assert "test prompt" in output


# ── CLI: gateway agents move text rendering ───────────────────────────


class TestAgentsMoveTextRendering:
    def test_text_with_previous(self, monkeypatch):
        payload = {
            "active_space_id": "sp-2",
            "active_space_name": "New",
            "previous_space_id": "sp-1",
            "previous_space_name": "Old",
        }
        monkeypatch.setattr(gw_cmd, "_move_managed_agent_space", lambda name, sid, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "revert" in output.lower()


# ── CLI: gateway agents doctor text rendering ─────────────────────────


class TestAgentsDoctorTextRendering:
    def test_text_passed(self, monkeypatch):
        payload = {
            "status": "passed",
            "summary": "all good",
            "checks": [{"name": "check1", "status": "passed", "detail": "ok"}],
        }
        monkeypatch.setattr(gw_cmd, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1"])
        assert result.exit_code == 0

    def test_text_failed(self, monkeypatch):
        payload = {
            "status": "failed",
            "summary": "problem",
            "checks": [{"name": "check1", "status": "failed", "detail": "bad"}],
        }
        monkeypatch.setattr(gw_cmd, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1"])
        assert result.exit_code == 0


# ── CLI: gateway agents archive/restore text rendering ────────────────


class TestAgentsArchiveRestoreTextRendering:
    def test_archive_text_multiple(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "bot2"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output
        assert "bot2" in output

    def test_restore_text_multiple(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_restore_managed_agent", lambda name: {"name": name, "desired_state": "stopped"})
        result = runner.invoke(app, ["gateway", "agents", "restore", "bot1", "bot2"])
        assert result.exit_code == 0

    def test_archive_mixed_results(self, monkeypatch):
        call_count = {"n": 0}

        def _archive(name, **kw):
            call_count["n"] += 1
            if name == "nope":
                raise LookupError("not found")
            return {"name": name}

        monkeypatch.setattr(gw_cmd, "_archive_managed_agent", _archive)
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "nope"])
        assert result.exit_code == 0  # partial success
        output = _strip(result.output)
        assert "bot1" in output
        assert "not found" in output.lower()


# ── CLI: gateway agents recover text rendering ────────────────────────


class TestAgentsRecoverTextRendering:
    def test_text_recovered(self, monkeypatch):
        payload = {
            "recovered": [{"name": "bot1", "agent_id": "a1"}],
            "already_present": [],
            "no_evidence": [],
            "count": 1,
        }
        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code == 0
        assert "Recovered" in _strip(result.output)

    def test_text_already_present(self, monkeypatch):
        payload = {
            "recovered": [],
            "already_present": ["bot1"],
            "no_evidence": [],
            "count": 0,
        }
        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        # exit_code 0 because already_present + no no_evidence = graceful
        assert result.exit_code == 0
        assert "Already present" in _strip(result.output)

    def test_text_no_evidence_exits_1(self, monkeypatch):
        payload = {
            "recovered": [],
            "already_present": [],
            "no_evidence": ["bot1"],
            "count": 0,
        }
        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code != 0


# ── CLI: gateway local init ──────────────────────────────────────────


class TestLocalInitCommand:
    def test_json_no_connect(self, monkeypatch, tmp_path):
        result = runner.invoke(
            app,
            [
                "gateway",
                "local",
                "init",
                "bot1",
                "--workdir",
                str(tmp_path),
                "--no-connect",
                "--force",
                "--json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent_name"] == "bot1"
        assert data["token_stored"] is False
        config = (tmp_path / ".ax" / "config.toml").read_text()
        assert 'agent_name = "bot1"' in config

    def test_already_exists_without_force(self, monkeypatch, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text("existing")
        result = runner.invoke(
            app,
            ["gateway", "local", "init", "bot1", "--workdir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "already exists" in _strip(result.output).lower() or "force" in _strip(result.output).lower()


# ── CLI: gateway spaces use ──────────────────────────────────────────


class TestSpacesUseCommand:
    def test_json(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "token": "axp_u_x", "base_url": "https://paxai.app"},
        )
        monkeypatch.setattr(gw_cmd, "_load_gateway_user_client", lambda: MagicMock())
        monkeypatch.setattr(gw_cmd, "resolve_space_id", lambda client, explicit: "sp-2")
        monkeypatch.setattr(gw_cmd, "_space_name_for_id", lambda client, sid: "New Space")
        monkeypatch.setattr(gw_cmd, "save_gateway_session", lambda s: Path("/tmp/session.json"))
        monkeypatch.setattr(gw_cmd, "upsert_space_cache_entry", lambda *a, **kw: None)
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "spaces", "use", "sp-2", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["space_id"] == "sp-2"


# ── CLI: gateway agents list text rendering ───────────────────────────


class TestAgentsListTextRendering:
    def test_text_with_hidden_hint(self, monkeypatch):
        payload = {
            "agents": [{"name": "bot1", "runtime_type": "echo", "template_id": "echo_test"}],
            "summary": {
                "managed_agents": 1,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 2,
                "hidden_agents": 1,
                "system_agents": 1,
            },
        }
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "--all" in output or "archived" in output.lower()

    def test_archived_only_filter(self, monkeypatch):
        payload = {
            "agents": [
                {"name": "bot1", "lifecycle_phase": "active", "runtime_type": "echo", "template_id": "echo_test"},
                {"name": "bot2", "lifecycle_phase": "archived", "runtime_type": "echo", "template_id": "echo_test"},
            ],
            "summary": {
                "managed_agents": 2,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 1,
                "hidden_agents": 0,
                "system_agents": 0,
            },
        }
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list", "--archived", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["agents"][0]["name"] == "bot2"
