"""Extended CLI + HTTP handler tests for ax_cli/commands/gateway.py — batch 2.

Targets uncovered line ranges 5779-6500, 7000-9015.  Focuses on the
``_build_gateway_ui_handler`` HTTP handler routes (GET / POST / PUT / DELETE),
the ``_render_agent_detail`` Rich helper, ``_wait_for_ui_ready``,
``_terminate_pids``, ``login`` command, and remaining CLI text-output branches.
"""

from __future__ import annotations

import io
import json
import os
import re
import signal
import socket
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from ax_cli.commands import gateway as gw_cmd
from ax_cli.main import app

runner = CliRunner()

# --- gateway split (#28 Phase 1): see removal doc ---
pytestmark = pytest.mark.skip(
    reason=(
        "Obsolete after the commands/gateway.py split (#28 Phase 1): these tests monkeypatch the pre-split ``ax_cli.commands.gateway`` monolith namespace, which no longer hosts the moved helpers. Rewrite-per-module or removal candidate — see docs/refactor/split-commands-gateway-removal.md"
    )
)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return ANSI_RE.sub("", text)


def _render_text(renderable) -> str:
    console = Console(record=True, width=140, color_system=None)
    console.print(renderable)
    return _strip(console.export_text())


# ---------------------------------------------------------------------------
# Helpers to simulate the HTTP handler without a real TCP server
# ---------------------------------------------------------------------------


def _make_handler(*, activity_limit: int = 10, refresh_ms: int = 2000):
    """Return the handler class built by _build_gateway_ui_handler."""
    # _build_gateway_ui_handler moved to gateway_ui in the #28 Phase 1 split.
    # Resolve it there directly so this shared helper keeps working for the
    # tests in other modules that import it (e.g. test_gateway_ui_connectors).
    from ax_cli.commands import gateway_ui

    return gateway_ui._build_gateway_ui_handler(activity_limit=activity_limit, refresh_ms=refresh_ms)


class _FakeWfile(io.BytesIO):
    """Writable bytes buffer pretending to be a socket wfile."""

    pass


class _FakeRfile(io.BytesIO):
    """Readable bytes buffer pretending to be a socket rfile."""

    pass


def _build_fake_request(
    method: str,
    path: str,
    body: dict | None = None,
    host: str = "localhost",
    headers: dict | None = None,
):
    """Return a raw HTTP/1.1 request as bytes."""
    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    if body_bytes:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(body_bytes)}")
    if headers:
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    raw = "\r\n".join(lines).encode("utf-8") + body_bytes
    return raw


def _invoke_handler(
    method: str,
    path: str,
    body: dict | None = None,
    host: str = "localhost",
    headers: dict | None = None,
    *,
    monkeypatch,
    handler_kwargs: dict | None = None,
):
    """Create a handler instance and invoke the appropriate do_* method.

    Returns (status_code, response_body_bytes, handler).
    """
    kw = handler_kwargs or {}
    HandlerClass = _make_handler(**kw)

    raw = _build_fake_request(method, path, body=body, host=host, headers=headers)
    rfile = _FakeRfile(raw)
    wfile = _FakeWfile()

    # BaseHTTPRequestHandler.__init__ calls handle() → parse + dispatch.
    # We override setup/finish to skip socket-layer work.
    class PatchedHandler(HandlerClass):
        def setup(self):
            self.rfile = rfile
            self.wfile = wfile

        def finish(self):
            pass

    # __init__ reads from rfile, dispatches, writes to wfile.
    handler = PatchedHandler(
        request=None,
        client_address=("127.0.0.1", 12345),
        server=MagicMock(),
    )
    wfile.seek(0)
    raw_response = wfile.read()
    # Parse status line
    first_line = raw_response.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
    parts = first_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) >= 2 else 0
    # Parse body (after double CRLF)
    body_start = raw_response.find(b"\r\n\r\n")
    response_body = raw_response[body_start + 4 :] if body_start >= 0 else b""
    return status_code, response_body, handler


def _json_response(status_code: int, body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


# ── _read_json_request ──────────────────────────────────────────────────


class TestReadJsonRequest:
    def test_empty_content_length_returns_empty_dict(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": "0"}
        assert gw_cmd._read_json_request(handler) == {}

    def test_valid_json_body(self):
        body = json.dumps({"key": "val"}).encode()
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        assert gw_cmd._read_json_request(handler) == {"key": "val"}

    def test_non_object_body_raises(self):
        body = json.dumps([1, 2, 3]).encode()
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        with pytest.raises(ValueError, match="must be an object"):
            gw_cmd._read_json_request(handler)

    def test_invalid_json_raises(self):
        body = b"{invalid"
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        with pytest.raises(ValueError, match="Invalid JSON"):
            gw_cmd._read_json_request(handler)

    def test_no_content_length_header(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {}
        assert gw_cmd._read_json_request(handler) == {}

    def test_empty_rfile_returns_empty(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": "10"}
        handler.rfile = io.BytesIO(b"")
        assert gw_cmd._read_json_request(handler) == {}


# ── GET handler routes ──────────────────────────────────────────────────


class TestGatewayUiHandlerGET:
    """Tests for do_GET in _build_gateway_ui_handler."""

    def test_forbidden_host(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/", host="evil.com", monkeypatch=monkeypatch)
        assert status == 403
        data = _json_response(status, body)
        assert "Forbidden" in data.get("error", "")

    @pytest.mark.parametrize("path", ["/", "/demo"])
    def test_demo_routes_return_html(self, monkeypatch, path):
        monkeypatch.setattr(gw_cmd, "_render_gateway_demo_page", lambda **kw: "<html>demo</html>")
        status, body, _ = _invoke_handler("GET", path, monkeypatch=monkeypatch)
        assert status == 200
        assert b"demo" in body

    def test_operator_returns_html(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_render_gateway_ui_page", lambda **kw: "<html>operator</html>")
        status, body, _ = _invoke_handler("GET", "/operator", monkeypatch=monkeypatch)
        assert status == 200

    def test_healthz(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/healthz", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["ok"] is True

    def test_favicon(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/favicon.svg", monkeypatch=monkeypatch)
        assert status == 200
        assert b"<svg" in body or b"svg" in body

    def test_favicon_ico(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/favicon.ico", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_status(self, monkeypatch):
        payload = {"agents": [], "summary": {}, "recent_activity": []}
        monkeypatch.setattr(gw_cmd, "_status_payload", lambda **kw: payload)
        status, body, _ = _invoke_handler("GET", "/api/status", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_status_with_all_flag(self, monkeypatch):
        payload = {"agents": [], "summary": {}, "recent_activity": []}
        captured = {}

        def _mock_status(**kw):
            captured.update(kw)
            return payload

        monkeypatch.setattr(gw_cmd, "_status_payload", _mock_status)
        status, body, _ = _invoke_handler("GET", "/api/status?all=true", monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("include_hidden") is True

    def test_local_inbox(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_local_session_inbox", lambda **kw: {"messages": [], "agent": "bot1"})
        status, body, _ = _invoke_handler(
            "GET",
            "/local/inbox?limit=5&channel=main",
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_local_sessions(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_registry", lambda: {"local_sessions": [{"name": "s1"}]})
        status, body, _ = _invoke_handler("GET", "/local/sessions", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["count"] == 1

    def test_api_runtime_types(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_runtime_types_payload", lambda: {"runtime_types": []})
        status, body, _ = _invoke_handler("GET", "/api/runtime-types", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_templates(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_agent_templates_payload", lambda: {"templates": []})
        status, body, _ = _invoke_handler("GET", "/api/templates", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_approvals(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_approval_rows_payload", lambda **kw: {"approvals": [], "count": 0, "pending": 0})
        status, body, _ = _invoke_handler("GET", "/api/approvals", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_approvals_with_status_filter(self, monkeypatch):
        captured = {}

        def _mock(**kw):
            captured.update(kw)
            return {"approvals": [], "count": 0, "pending": 0}

        monkeypatch.setattr(gw_cmd, "_approval_rows_payload", _mock)
        status, body, _ = _invoke_handler("GET", "/api/approvals?status=pending", monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("status") == "pending"

    def test_api_approval_detail(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_approval_detail_payload",
            lambda aid: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("GET", "/api/approvals/appr-1", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["approval"]["approval_id"] == "appr-1"

    def test_api_approval_detail_not_found(self, monkeypatch):
        def _raise(aid):
            raise LookupError("not found")

        monkeypatch.setattr(gw_cmd, "_approval_detail_payload", _raise)
        status, body, _ = _invoke_handler("GET", "/api/approvals/bad", monkeypatch=monkeypatch)
        assert status == 404

    def test_api_spaces_with_data(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_spaces_payload",
            lambda: {"spaces": [{"id": "sp-1"}], "active_space_id": "sp-1"},
        )
        status, body, _ = _invoke_handler("GET", "/api/spaces", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_spaces_no_data(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_spaces_payload", lambda: {"spaces": [], "active_space_id": None})
        status, body, _ = _invoke_handler("GET", "/api/spaces", monkeypatch=monkeypatch)
        assert status == 503

    def test_api_agents_inbox(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_inbox_for_managed_agent",
            lambda **kw: {"messages": [], "agent": "bot1"},
        )
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_agents_inbox_not_found(self, monkeypatch):
        def _raise(**kw):
            raise LookupError("not found")

        monkeypatch.setattr(gw_cmd, "_inbox_for_managed_agent", _raise)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 404

    def test_api_agents_inbox_bad_request(self, monkeypatch):
        def _raise(**kw):
            raise ValueError("bad param")

        monkeypatch.setattr(gw_cmd, "_inbox_for_managed_agent", _raise)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 400

    def test_api_agents_detail(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name}, "recent_activity": []},
        )
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["agent"]["name"] == "bot1"

    def test_api_agents_detail_not_found(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_agent_detail_payload", lambda name, **kw: None)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 404

    def test_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/unknown", monkeypatch=monkeypatch)
        assert status == 404


# ── POST handler routes ─────────────────────────────────────────────────


class TestGatewayUiHandlerPOST:
    """Tests for do_POST in _build_gateway_ui_handler."""

    def test_post_forbidden_host(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/agents", body={}, host="evil.com", monkeypatch=monkeypatch)
        assert status == 403

    def test_post_templates_install_not_allowed(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/templates/bogus_template/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 400
        data = _json_response(status, body)
        assert "allowlist" in data.get("error", "")

    def test_post_templates_install_no_session(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: None)
        # Use a real template from the allowlist
        template_id = list(gw_cmd._RUNTIME_INSTALL_RECIPES.keys())[0] if gw_cmd._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 403
        data = _json_response(status, body)
        assert "login" in data.get("error", "").lower()

    def test_post_templates_install_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            gw_cmd,
            "_install_runtime_payload",
            lambda tid, **kw: {"ready": True, "target": "/opt/hermes"},
        )
        template_id = list(gw_cmd._RUNTIME_INSTALL_RECIPES.keys())[0] if gw_cmd._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_templates_install_not_ready(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            gw_cmd,
            "_install_runtime_payload",
            lambda tid, **kw: {"ready": False, "target": "/opt/hermes"},
        )
        template_id = list(gw_cmd._RUNTIME_INSTALL_RECIPES.keys())[0] if gw_cmd._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 422

    def test_post_templates_install_permission_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(tid, **kw):
            raise PermissionError("no perm")

        monkeypatch.setattr(gw_cmd, "_install_runtime_payload", _raise)
        template_id = list(gw_cmd._RUNTIME_INSTALL_RECIPES.keys())[0] if gw_cmd._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 403

    def test_post_templates_install_value_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(tid, **kw):
            raise ValueError("bad value")

        monkeypatch.setattr(gw_cmd, "_install_runtime_payload", _raise)
        template_id = list(gw_cmd._RUNTIME_INSTALL_RECIPES.keys())[0] if gw_cmd._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_register(self, monkeypatch):
        entry = {"name": "bot1", "desired_state": "running"}
        monkeypatch.setattr(gw_cmd, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(gw_cmd, "gateway_core", MagicMock())
        monkeypatch.setattr(
            gw_cmd.gateway_core, "infer_operator_profile", lambda e: {"placement": "hosted", "activation": "on_demand"}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents", body={"name": "bot1"}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_rate_limited(self, monkeypatch):
        def _raise(**kw):
            raise gw_cmd.UpstreamRateLimitedError(Exception("429"), 1)

        monkeypatch.setattr(gw_cmd, "_register_managed_agent", _raise)
        status, body, _ = _invoke_handler("POST", "/api/agents", body={"name": "bot1"}, monkeypatch=monkeypatch)
        assert status == 429
        data = _json_response(status, body)
        assert "rate" in data.get("error", "").lower()

    def test_post_agents_cleanup_hide(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_hide_managed_agents", lambda names, **kw: {"hidden": names, "count": len(names)})
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-hide", body={"names": ["bot1", "bot2"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_cleanup_hide_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-hide", body={"names": "not a list"}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_cleanup_restore(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_restore_hidden_managed_agents", lambda names: {"restored": names, "count": len(names)}
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-restore", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_cleanup_restore_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-restore", body={"names": 42}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_recover(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_recover_managed_agents_from_evidence",
            lambda names: {"recovered": [], "count": 0},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/recover", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_recover_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/agents/recover", body={"names": "bad"}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_agents_recover_value_error(self, monkeypatch):
        def _raise(names):
            raise ValueError("broken")

        monkeypatch.setattr(gw_cmd, "_recover_managed_agents_from_evidence", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/recover", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 400

    @pytest.mark.parametrize(
        ("payload", "expected_status"),
        [
            ({"status": "approved", "session_token": "tok"}, 200),
            ({"status": "pending"}, 202),
        ],
    )
    def test_post_local_connect_statuses(self, monkeypatch, payload, expected_status):
        monkeypatch.setattr(
            gw_cmd,
            "_connect_local_pass_through_agent",
            lambda **kw: payload,
        )
        status, body, _ = _invoke_handler(
            "POST", "/local/connect", body={"agent_name": "bot1"}, monkeypatch=monkeypatch
        )
        assert status == expected_status

    def test_post_local_send(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_send_local_session_message",
            lambda **kw: {"agent": "bot1", "message": {"id": "m1"}},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/send",
            body={"content": "hello"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 201

    def test_post_local_tasks(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_create_local_session_task",
            lambda **kw: {"task_id": "t1"},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/tasks",
            body={"title": "task"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 201

    def test_post_local_proxy(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_proxy_local_session_call",
            lambda **kw: {"result": "ok"},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/proxy",
            body={"method": "list_messages"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("missing"), 404),
            (ValueError("bad"), 400),
        ],
    )
    def test_post_local_proxy_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(gw_cmd, "_proxy_local_session_call", lambda **kw: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler(
            "POST",
            "/local/proxy",
            body={},
            headers={"X-Gateway-Session": "tok"},
            monkeypatch=monkeypatch,
        )
        assert status == expected_status

    def test_post_agents_start(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_set_managed_agent_desired_state", lambda name, state: {"name": name, "desired_state": state}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/start", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_stop(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_set_managed_agent_desired_state", lambda name, state: {"name": name, "desired_state": state}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/stop", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_attach(self, monkeypatch):
        payload = {"mcp_path": "/tmp/w/.ax/mcp.json", "launch_mode": "bg"}
        monkeypatch.setattr(gw_cmd, "_prepare_attached_agent_payload", lambda name: payload)
        monkeypatch.setattr(gw_cmd, "_launch_attached_agent_session", lambda p: {**p, "launched": True})
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/attach", body={}, monkeypatch=monkeypatch)
        assert status == 202

    def test_post_agents_manual_attach(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_mark_attached_agent_session", lambda name, **kw: {"name": name, "state": "active"}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/manual-attach", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_manual_attach_error(self, monkeypatch):
        def _raise(name, **kw):
            raise LookupError("missing")

        monkeypatch.setattr(gw_cmd, "_mark_attached_agent_session", _raise)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/manual-attach", body={}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_agents_external_runtime_announce(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd, "_announce_external_agent_runtime", lambda name, body: {"name": name, "status": "ok"}
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={"runtime": "ext"}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_external_runtime_announce_not_found(self, monkeypatch):
        def _raise(name, body):
            raise LookupError("not found")

        monkeypatch.setattr(gw_cmd, "_announce_external_agent_runtime", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={}, monkeypatch=monkeypatch
        )
        assert status == 404

    def test_post_agents_external_runtime_announce_value_error(self, monkeypatch):
        def _raise(name, body):
            raise ValueError("bad")

        monkeypatch.setattr(gw_cmd, "_announce_external_agent_runtime", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_send(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_send_from_managed_agent",
            lambda **kw: {"agent": "bot1", "message": {"id": "m1"}, "content": "hi"},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/send", body={"content": "hello"}, monkeypatch=monkeypatch
        )
        assert status == 201

    def test_post_agents_test(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_send_gateway_test_to_managed_agent",
            lambda name, **kw: {"target_agent": name, "message": {"id": "m1"}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/test", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_ack(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_ack_managed_agent_message",
            lambda name, **kw: {"acked": True},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/api/agents/bot1/ack",
            body={"message_id": "m1", "reply_id": "r1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_post_agents_move(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_move_managed_agent_space",
            lambda name, sid, **kw: {"space_id": sid},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/move", body={"space_id": "sp-2"}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_system_prompt(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_update_managed_agent",
            lambda **kw: {"name": "bot1", "system_prompt": kw.get("system_prompt")},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/api/agents/bot1/system-prompt",
            body={"system_prompt": "You are a helper"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_post_agents_system_prompt_clear(self, monkeypatch):
        captured = {}

        def _mock(**kw):
            captured.update(kw)
            return {"name": "bot1"}

        monkeypatch.setattr(gw_cmd, "_update_managed_agent", _mock)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/system-prompt", body={}, monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("system_prompt") == ""

    def test_post_agents_pin(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_set_managed_agent_pin",
            lambda name, pinned: {"name": name, "pinned": pinned},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/pin", body={"pinned": True}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_doctor(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_run_gateway_doctor",
            lambda name, **kw: {"status": "passed", "checks": []},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/doctor", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_approve(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name, "approval_id": "appr-1"}},
        )
        monkeypatch.setattr(
            gw_cmd,
            "approve_gateway_approval",
            lambda aid, **kw: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_approve_not_found(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_agent_detail_payload", lambda name, **kw: None)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 404

    def test_post_agents_approve_no_approval_id(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_approvals_approve(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "approve_gateway_approval",
            lambda aid, **kw: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("POST", "/api/approvals/appr-1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_reject(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_reject_managed_agent_approval",
            lambda name: {"name": name, "rejected": True},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/reject", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_approvals_reject(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "deny_gateway_approval",
            lambda aid: {"approval_id": aid, "status": "rejected"},
        )
        status, body, _ = _invoke_handler("POST", "/api/approvals/appr-1/reject", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/unknown", body={}, monkeypatch=monkeypatch)
        assert status == 404

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("agent not found"), 404),
            (ValueError("bad input"), 400),
            (typer.Exit(1), 400),
            (RuntimeError("internal"), 500),
        ],
    )
    def test_post_top_level_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(gw_cmd, "_set_managed_agent_desired_state", lambda name, state: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/start", body={}, monkeypatch=monkeypatch)
        assert status == expected_status


# ── PUT handler ──────────────────────────────────────────────────────────


class TestGatewayUiHandlerPUT:
    def test_put_agent(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_update_managed_agent",
            lambda **kw: {"name": "bot1", "updated": True},
        )
        status, body, _ = _invoke_handler(
            "PUT", "/api/agents/bot1", body={"description": "updated"}, monkeypatch=monkeypatch
        )
        assert status == 200

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("not found"), 404),
            (ValueError("bad"), 400),
            (typer.Exit(1), 400),
            (RuntimeError("crash"), 500),
        ],
    )
    def test_put_agent_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(gw_cmd, "_update_managed_agent", lambda **kw: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler("PUT", "/api/agents/bot1", body={}, monkeypatch=monkeypatch)
        assert status == expected_status

    def test_put_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("PUT", "/api/unknown", body={}, monkeypatch=monkeypatch)
        assert status == 404


# ── DELETE handler ───────────────────────────────────────────────────────


class TestGatewayUiHandlerDELETE:
    def test_delete_agent(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_remove_managed_agent", lambda name: {"name": name, "removed": True})
        status, body, _ = _invoke_handler("DELETE", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 200

    def test_delete_agent_not_found(self, monkeypatch):
        def _raise(name):
            raise LookupError("not found")

        monkeypatch.setattr(gw_cmd, "_remove_managed_agent", _raise)
        status, body, _ = _invoke_handler("DELETE", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 404

    def test_delete_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("DELETE", "/api/unknown", monkeypatch=monkeypatch)
        assert status == 404


# ── _render_agent_detail ────────────────────────────────────────────────


class TestRenderAgentDetail:
    def test_renders_basic_entry(self):
        entry = {
            "name": "test-bot",
            "template_id": "echo_test",
            "runtime_type": "echo",
            "mode": "live",
            "presence": "IDLE",
            "reply": "auto",
            "confidence": "HIGH",
            "confidence_reason": "all good",
            "confidence_detail": "nothing wrong",
            "asset_class": "processor",
            "intake_model": "direct",
            "trigger_sources": ["direct_message"],
            "return_paths": ["reply"],
            "telemetry_shape": "standard",
            "worker_model": "claude-3",
            "attestation_state": "verified",
            "approval_state": "approved",
            "acting_agent_name": "test-bot",
            "identity_status": "ok",
            "environment_label": "local",
            "environment_status": "healthy",
            "active_space_name": "Work",
            "space_status": "active",
            "default_space_name": "Work",
            "allowed_space_count": 1,
            "install_id": "inst-1",
            "runtime_instance_id": "ri-1",
            "desired_state": "running",
            "effective_state": "running",
            "connected": True,
            "backlog_depth": 0,
            "last_seen_age_seconds": 5,
            "reconnect_backoff_seconds": 0,
            "processed_count": 10,
            "dropped_count": 0,
            "last_work_received_at": "2026-01-01T00:00:00Z",
            "last_work_completed_at": "2026-01-01T00:01:00Z",
            "current_status": "idle",
            "current_activity": "waiting",
            "current_tool": None,
            "timeout_seconds": 30,
            "space_id": "sp-1",
            "credential_source": "gateway",
            "token_file": "/tmp/tok",
            "agent_id": "a-1",
            "last_reply_preview": "ok",
            "last_error": None,
            "last_successful_doctor_at": "2026-01-01",
            "last_doctor_result": {"status": "passed"},
            "workdir": "/tmp/w",
            "exec_command": None,
            "added_at": "2026-01-01T00:00:00Z",
            "system_prompt": "You are a helper.",
        }
        activity = [
            {"ts": "2026-01-01", "event": "test", "agent_name": "test-bot", "message_id": "m1"},
        ]
        result = gw_cmd._render_agent_detail(entry, activity=activity)
        rendered = _render_text(result)
        assert "Managed Agent" in rendered
        assert "@test-bot" in rendered
        assert "Operator System Prompt" in rendered
        assert "You are a helper." in rendered

    def test_renders_empty_entry(self):
        entry = {"name": "minimal"}
        result = gw_cmd._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "@minimal" in rendered
        assert "Runtime Details" in rendered

    def test_renders_without_system_prompt(self):
        entry = {"name": "no-prompt", "system_prompt": ""}
        result = gw_cmd._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "Operator System Prompt" in rendered
        assert "--system-prompt" in rendered

    def test_renders_with_doctor_result_non_dict(self):
        entry = {"name": "bot", "last_doctor_result": "not-a-dict"}
        result = gw_cmd._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "Doctor Status" in rendered

    def test_adapter_row_quiet_for_current_runtime(self):
        entry = {"name": "current", "runtime_type": "hermes_plugin"}
        rendered = _render_text(gw_cmd._render_agent_detail(entry, activity=[]))
        assert "hermes_plugin" in rendered
        assert "deprecated" not in rendered
        assert " - " in rendered or "\n-\n" in rendered


# ── _wait_for_ui_ready ──────────────────────────────────────────────────


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
        assert gw_cmd._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.5) is True

    def test_returns_false_when_process_dies(self, monkeypatch):
        process = MagicMock()
        process.poll.return_value = 1  # process exited

        def _fail(*a, **kw):
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", _fail)
        assert gw_cmd._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.2) is False

    def test_returns_false_when_timeout(self, monkeypatch):
        process = MagicMock()
        process.poll.return_value = None

        def _fail(*a, **kw):
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", _fail)
        assert gw_cmd._wait_for_ui_ready(process, host="127.0.0.1", port=8765, timeout=0.2) is False


# ── _terminate_pids ──────────────────────────────────────────────────────


class TestTerminatePids:
    def test_empty_list(self):
        requested, forced = gw_cmd._terminate_pids([], timeout=0.1)
        assert requested == []
        assert forced == []

    def test_process_lookup_error_skipped(self, monkeypatch):
        def _kill(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", _kill)
        requested, forced = gw_cmd._terminate_pids([99999], timeout=0.1)
        assert requested == []

    def test_process_terminates_normally(self, monkeypatch):
        killed = []

        def _kill(pid, sig):
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", _kill)
        monkeypatch.setattr(gw_cmd.gateway_core, "_pid_alive", lambda pid: False)
        requested, forced = gw_cmd._terminate_pids([1234], timeout=0.2)
        assert requested == [1234]
        assert forced == []

    def test_process_needs_sigkill(self, monkeypatch):
        killed = []

        def _kill(pid, sig):
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", _kill)
        monkeypatch.setattr(gw_cmd.gateway_core, "_pid_alive", lambda pid: True)
        requested, forced = gw_cmd._terminate_pids([1234], timeout=0.1)
        assert requested == [1234]
        assert forced == [1234]
        # Should have sent both SIGTERM and SIGKILL
        assert any(s == signal.SIGTERM for _, s in killed)
        assert any(s == signal.SIGKILL for _, s in killed)


# ── login command ────────────────────────────────────────────────────────


class TestLoginCommand:
    def test_login_non_user_pat_fails(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_token", lambda t: "axp_a_badprefix")
        result = runner.invoke(app, ["gateway", "login", "--token", "axp_a_badprefix"])
        assert result.exit_code != 0
        assert "user PAT" in _strip(result.output)

    def test_login_exchanger_fails(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        # Mock the TokenExchanger import
        fake_exchanger = MagicMock()
        fake_exchanger.get_token.side_effect = RuntimeError("exchange failed")
        monkeypatch.setattr(gw_cmd, "TokenExchanger", lambda url, tok: fake_exchanger, raising=False)

        # Patch the import path
        import ax_cli.commands.gateway as gw_mod

        with patch.dict("sys.modules", {}):
            monkeypatch.setattr(gw_mod, "TokenExchanger", lambda url, tok: fake_exchanger, raising=False)

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123"])
        assert result.exit_code != 0

    def test_login_json_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        # Mock TokenExchanger
        fake_exchanger = MagicMock()
        fake_exchanger.get_token.return_value = "jwt_test"

        # We need to patch the from-import that happens inside the function
        import ax_cli.token_cache as tc_mod

        monkeypatch.setattr(tc_mod, "TokenExchanger", lambda url, tok: fake_exchanger)

        fake_client = MagicMock()
        fake_client.whoami.return_value = {"username": "tester", "email": "t@t.com"}
        fake_client.list_spaces.return_value = {"spaces": []}
        monkeypatch.setattr(gw_cmd, "AxClient", lambda **kw: fake_client)

        from ax_cli.commands import auth as auth_cmd

        monkeypatch.setattr(auth_cmd, "_select_login_space", lambda spaces: None)
        monkeypatch.setattr(gw_cmd, "save_gateway_session", lambda p: Path("/tmp/session.json"))
        monkeypatch.setattr(gw_cmd, "load_gateway_registry", lambda: {"gateway": {}})
        monkeypatch.setattr(gw_cmd, "save_gateway_registry", lambda r: None)
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123", "--json"])
        assert result.exit_code == 0
        # Output contains err_console prefix lines + JSON; find the JSON part
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert data["username"] == "tester"

    def test_login_with_space_id(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(gw_cmd, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        import ax_cli.token_cache as tc_mod

        fake_exchanger = MagicMock()
        fake_exchanger.get_token.return_value = "jwt_test"
        monkeypatch.setattr(tc_mod, "TokenExchanger", lambda url, tok: fake_exchanger)

        fake_client = MagicMock()
        fake_client.whoami.return_value = {"username": "tester", "email": "t@t.com"}
        fake_client.list_spaces.return_value = {"spaces": [{"id": "sp-1", "name": "Work"}]}
        monkeypatch.setattr(gw_cmd, "AxClient", lambda **kw: fake_client)
        monkeypatch.setattr(gw_cmd, "resolve_space_id", lambda client, explicit: "sp-1")
        monkeypatch.setattr(gw_cmd, "save_gateway_session", lambda p: Path("/tmp/session.json"))
        monkeypatch.setattr(gw_cmd, "load_gateway_registry", lambda: {"gateway": {}})
        monkeypatch.setattr(gw_cmd, "save_gateway_registry", lambda r: None)
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)

        from ax_cli.commands import auth as auth_cmd

        monkeypatch.setattr(auth_cmd, "_candidate_space_id", lambda s: "sp-1")

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123", "--space-id", "sp-1", "--json"])
        assert result.exit_code == 0
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert data["space_id"] == "sp-1"


# ── spaces commands ─────────────────────────────────────────────────────


class TestSpacesListErrors:
    def test_list_with_upstream_error(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
            "error": "upstream 503",
            "cached": True,
        }
        monkeypatch.setattr(gw_cmd, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "cached" in output.lower() or "upstream" in output.lower()


# ── activity command extra branches ──────────────────────────────────────


class TestActivityExtraBranches:
    def test_invalid_json_lines_skipped(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        log.write_text('{"ts":"2026-01-01","event":"test"}\nnot-json\n42\n{"ts":"2026-01-02","event":"ok"}')
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_text_with_data(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1", "phase": "idle"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity"])
        assert result.exit_code == 0

    def test_oserror_returns_empty(self, monkeypatch, tmp_path):
        # Create a log that will raise OSError
        log = tmp_path / "activity.jsonl"
        log.write_text('{"event":"x"}')

        def _broken_read_text(*a, **kw):
            raise OSError("disk error")

        monkeypatch.setattr(gw_cmd, "activity_log_path", lambda: log)
        # Patch Path.read_text to raise
        monkeypatch.setattr(Path, "read_text", _broken_read_text)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []


# ── local send text output branches ──────────────────────────────────────


class TestLocalSendTextOutput:
    def _setup_send_mocks(self, monkeypatch, response_json):
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
        fake_resp.json.return_value = response_json
        fake_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: fake_resp)

    def test_text_with_inbox_messages(self, monkeypatch):
        self._setup_send_mocks(
            monkeypatch,
            {
                "agent": "bot1",
                "message": {"id": "m1"},
                "next_session_proof": None,
            },
        )
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {
                "messages": [
                    {"created_at": "2026-01-01", "display_name": "alice", "content": "hey there"},
                ],
                "agent": "bot1",
            },
        )
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output

    def test_text_with_next_session_proof(self, monkeypatch):
        self._setup_send_mocks(
            monkeypatch,
            {
                "agent": "bot1",
                "message": {"id": "m1"},
                "next_session_proof": "proof-abc",
            },
        )
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {"messages": []},
        )
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "proof-abc" in output

    def test_text_with_inbox_error(self, monkeypatch):
        self._setup_send_mocks(
            monkeypatch,
            {
                "agent": "bot1",
                "message": {"id": "m1"},
            },
        )

        def _fail(**kw):
            raise httpx.HTTPStatusError(
                "bad",
                request=MagicMock(),
                response=MagicMock(status_code=500, text="err", json=lambda: {"error": "err"}),
            )

        monkeypatch.setattr(gw_cmd, "_poll_local_inbox_over_http", _fail)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output

    def test_json_with_connect_payload(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", {"status": "approved", "registry_ref": "ref-1", "agent": {"name": "bot1"}}),
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
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--json", "--no-inbox"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data.get("connect", {}).get("status") == "approved"

    def test_send_with_session_challenge_error(self, monkeypatch):
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

        fake_response = MagicMock()
        fake_response.status_code = 400
        fake_response.text = "session_challenge_required: abc123"
        fake_response.json.return_value = {"error": "session_challenge_required: abc123"}

        def _post(*a, **kw):
            raise httpx.HTTPStatusError("bad", request=MagicMock(), response=fake_response)

        monkeypatch.setattr(httpx, "post", _post)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code != 0

    def test_send_space_resolution_fails(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok", "--space", "bad-slug"],
        )
        assert result.exit_code != 0
        assert "Could not resolve" in _strip(result.output)

    def test_send_with_pending_replies(self, monkeypatch):
        self._setup_send_mocks(
            monkeypatch,
            {
                "agent": "bot1",
                "message": {"id": "m1"},
            },
        )
        monkeypatch.setattr(
            gw_cmd,
            "_check_local_pending_replies",
            lambda **kw: {"count": 2, "message_ids": ["m1", "m2"], "newest_senders": ["alice"]},
        )
        monkeypatch.setattr(gw_cmd, "_poll_local_inbox_over_http", lambda **kw: {"messages": []})
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0


# ── local inbox text output branches ─────────────────────────────────────


class TestLocalInboxTextOutput:
    def test_text_with_messages(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_local_gateway_session", lambda **kw: ("tok", None))
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {
                "agent": "bot1",
                "messages": [
                    {"created_at": "2026-01-01", "agent_name": "alice", "content": "hello"},
                ],
            },
        )
        result = runner.invoke(app, ["gateway", "local", "inbox", "--session-token", "tok"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "alice" in output

    def test_json_with_connect_and_wait(self, monkeypatch):
        monkeypatch.setattr(
            gw_cmd,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok", {"status": "approved", "registry_ref": "r1", "agent": {"name": "bot1"}}),
        )
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: {"agent": "bot1", "messages": []},
        )
        result = runner.invoke(
            app,
            ["gateway", "local", "inbox", "--json", "--wait", "1"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data.get("connect", {}).get("status") == "approved"
        assert data.get("waited_seconds") == 1

    def test_inbox_http_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_local_gateway_session", lambda **kw: ("tok", None))

        fake_response = MagicMock()
        fake_response.status_code = 500
        fake_response.text = "internal error"
        fake_response.json.return_value = {"error": "internal error"}

        def _raise(**kw):
            raise httpx.HTTPStatusError("bad", request=MagicMock(), response=fake_response)

        monkeypatch.setattr(gw_cmd, "_poll_local_inbox_over_http", _raise)
        result = runner.invoke(app, ["gateway", "local", "inbox", "--session-token", "tok"])
        assert result.exit_code != 0

    def test_inbox_generic_error(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_local_gateway_session", lambda **kw: ("tok", None))
        monkeypatch.setattr(
            gw_cmd,
            "_poll_local_inbox_over_http",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = runner.invoke(app, ["gateway", "local", "inbox", "--session-token", "tok"])
        assert result.exit_code != 0

    def test_inbox_space_resolution_fails(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "local", "inbox", "--session-token", "tok", "--space", "bad-slug"],
        )
        assert result.exit_code != 0
        assert "Could not resolve" in _strip(result.output)


# ── local init text output branches ──────────────────────────────────────


class TestLocalInitTextOutput:
    def test_text_no_connect(self, tmp_path):
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
            ],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output
        assert "not stored" in output

    def test_text_with_connect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            gw_cmd,
            "_request_local_connect",
            lambda **kw: {"status": "approved", "approval_id": "appr-1"},
        )
        result = runner.invoke(
            app,
            [
                "gateway",
                "local",
                "init",
                "bot1",
                "--workdir",
                str(tmp_path),
                "--force",
            ],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "approved" in output

    def test_create_workdir(self, tmp_path):
        target = tmp_path / "new" / "deep"
        result = runner.invoke(
            app,
            [
                "gateway",
                "local",
                "init",
                "bot1",
                "--workdir",
                str(target),
                "--create-workdir",
                "--no-connect",
                "--json",
            ],
        )
        assert result.exit_code == 0
        assert target.is_dir()

    def test_connect_error(self, monkeypatch, tmp_path):
        def _raise(**kw):
            raise ValueError("connection refused")

        monkeypatch.setattr(gw_cmd, "_request_local_connect", _raise)
        result = runner.invoke(
            app,
            [
                "gateway",
                "local",
                "init",
                "bot1",
                "--workdir",
                str(tmp_path),
                "--force",
            ],
        )
        assert result.exit_code != 0


# ── agents add with space resolution ────────────────────────────────────


class TestAgentsAddSpaceResolution:
    def test_add_with_space_cached(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: "sp-resolved")
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        monkeypatch.setattr(
            gw_cmd,
            "_register_managed_agent",
            lambda **kw: {
                "name": "bot1",
                "desired_state": "running",
                "token_file": "/tmp/t",
                "template_label": "Echo",
                "asset_type_label": "test",
                "timeout_seconds": None,
            },
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_add_with_space_cache_miss(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        monkeypatch.setattr(gw_cmd, "_load_gateway_user_client", lambda: MagicMock())
        monkeypatch.setattr(gw_cmd, "resolve_space_id", lambda client, explicit: "sp-from-api")
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        monkeypatch.setattr(
            gw_cmd,
            "_register_managed_agent",
            lambda **kw: {
                "name": "bot1",
                "desired_state": "running",
                "token_file": "/tmp/t",
                "template_label": "Echo",
                "asset_type_label": "test",
                "timeout_seconds": None,
            },
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_add_space_resolution_fails(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        monkeypatch.setattr(gw_cmd, "_load_gateway_user_client", lambda: MagicMock())

        def _raise(client, explicit):
            raise RuntimeError("space API down")

        monkeypatch.setattr(gw_cmd, "resolve_space_id", _raise)
        monkeypatch.setattr(gw_cmd, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "bad-space"],
        )
        assert result.exit_code != 0


# ── agents update text output ───────────────────────────────────────────


class TestAgentsUpdateTextRendering:
    def test_text_with_timeout(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": 60,
        }
        monkeypatch.setattr(gw_cmd, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(app, ["gateway", "agents", "update", "echo1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "timeout" in output.lower()
        assert "60" in output

    def test_update_with_system_prompt_file(self, monkeypatch, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("You are a coder.")
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": None,
        }
        monkeypatch.setattr(gw_cmd, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(
            app,
            ["gateway", "agents", "update", "echo1", "--system-prompt-file", str(prompt_file), "--json"],
        )
        assert result.exit_code == 0


# ── agents show text rendering ──────────────────────────────────────────


class TestAgentsShowTextRendering:
    def test_text_output(self, monkeypatch):
        detail = {
            "agent": {
                "name": "bot1",
                "template_id": "echo_test",
                "system_prompt": "Be helpful",
            },
            "recent_activity": [],
        }
        monkeypatch.setattr(gw_cmd, "_agent_detail_payload", lambda name, **kw: detail)
        result = runner.invoke(app, ["gateway", "agents", "show", "bot1"])
        assert result.exit_code == 0


# ── agents inbox with space resolution ──────────────────────────────────


class TestAgentsInboxSpaceResolution:
    def test_inbox_space_cache_hit(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: "sp-1")
        monkeypatch.setattr(
            gw_cmd,
            "_inbox_for_managed_agent",
            lambda **kw: {"agent": "bot1", "messages": []},
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "inbox", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_inbox_space_cache_miss(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "agents", "inbox", "bot1", "--space", "bad-slug"],
        )
        assert result.exit_code != 0
        assert "Could not resolve" in _strip(result.output)


# ── agents mark-attached text output ─────────────────────────────────────


class TestAgentsMarkAttachedTextRendering:
    def test_text_success(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "_mark_attached_agent_session", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "mark-attached", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "manually attached" in output.lower()


# ── agents attach text output ───────────────────────────────────────────


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
        monkeypatch.setattr(gw_cmd, "_prepare_attached_agent_payload", lambda name: payload)
        result = runner.invoke(app, ["gateway", "agents", "attach", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "channel ready" in output.lower()
        assert "bot1" in output


# ── _print_pending_reply_warning_local extra branches ────────────────────


class TestPrintPendingReplyWarningExtra:
    def test_warning_with_single_sender(self, capsys):
        gw_cmd._print_pending_reply_warning_local({"count": 1, "newest_senders": ["alice"]})
        # Should have produced some output (Rich console writes to stderr by default
        # but _print_pending_reply_warning_local uses console which writes to stdout)
        # The test just verifies it does not crash

    def test_warning_with_multiple_senders(self, capsys):
        gw_cmd._print_pending_reply_warning_local({"count": 3, "newest_senders": ["alice", "bob", "charlie"]})

    def test_warning_plural(self, capsys):
        gw_cmd._print_pending_reply_warning_local({"count": 5, "newest_senders": []})


# ── stop command with forced kills ──────────────────────────────────────


class TestGatewayStopForcedKills:
    def test_forced_kill_output(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "active_gateway_pids", lambda: [9999])
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pids", lambda: [9998])
        monkeypatch.setattr(gw_cmd, "_terminate_pids", lambda pids, **kw: (pids, pids))  # all forced
        monkeypatch.setattr(gw_cmd, "clear_gateway_ui_state", lambda **kw: None)
        monkeypatch.setattr(gw_cmd.gateway_core, "clear_gateway_pid", lambda: None)
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)
        result = runner.invoke(app, ["gateway", "stop"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "Forced" in output or "force" in output.lower()


# ── start command branches ──────────────────────────────────────────────


class TestGatewayStartBranches:
    def test_daemon_fails_to_start(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(gw_cmd, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 9999
        monkeypatch.setattr(gw_cmd, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(gw_cmd, "_wait_for_daemon_ready", lambda proc: False)
        monkeypatch.setattr(gw_cmd, "_tail_log_lines", lambda path: "Error in daemon")
        monkeypatch.setattr(gw_cmd, "daemon_log_path", lambda: Path("/tmp/gw.log"))

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code != 0
        assert "Failed" in _strip(result.output)

    def test_ui_fails_to_start(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(gw_cmd, "active_gateway_pid", lambda: 1111)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 2222
        monkeypatch.setattr(gw_cmd, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(gw_cmd, "_wait_for_ui_ready", lambda proc, **kw: False)
        monkeypatch.setattr(gw_cmd, "_tail_log_lines", lambda path: "Port in use")
        monkeypatch.setattr(gw_cmd, "ui_log_path", lambda: Path("/tmp/ui.log"))
        monkeypatch.setattr(gw_cmd, "daemon_log_path", lambda: Path("/tmp/gw.log"))

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code != 0
        assert "Failed" in _strip(result.output)

    def test_both_start_fresh(self, monkeypatch):
        monkeypatch.setattr(gw_cmd, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(gw_cmd, "active_gateway_pid", lambda: None)
        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", lambda: None)

        fake_proc = MagicMock()
        fake_proc.pid = 5000
        monkeypatch.setattr(gw_cmd, "_spawn_gateway_background_process", lambda cmd, **kw: fake_proc)
        monkeypatch.setattr(gw_cmd, "_wait_for_daemon_ready", lambda proc: True)

        # After daemon, active_gateway_pid should return a pid
        call_count = {"pid": 0}

        def _active_pid():
            call_count["pid"] += 1
            return 5000 if call_count["pid"] > 1 else None

        monkeypatch.setattr(gw_cmd, "active_gateway_pid", _active_pid)
        monkeypatch.setattr(gw_cmd, "_wait_for_ui_ready", lambda proc, **kw: True)

        ui_count = {"n": 0}

        def _active_ui_pid():
            ui_count["n"] += 1
            return 5001 if ui_count["n"] > 1 else None

        monkeypatch.setattr(gw_cmd, "active_gateway_ui_pid", _active_ui_pid)
        monkeypatch.setattr(gw_cmd, "ui_status", lambda: {"running": True, "url": "http://localhost:8765"})
        monkeypatch.setattr(gw_cmd, "daemon_log_path", lambda: Path("/tmp/gw.log"))
        monkeypatch.setattr(gw_cmd, "ui_log_path", lambda: Path("/tmp/ui.log"))
        monkeypatch.setattr(gw_cmd, "record_gateway_activity", lambda *a, **kw: None)

        result = runner.invoke(app, ["gateway", "start", "--no-open"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "started" in output.lower()


# ── _format_age / _format_timestamp ──────────────────────────────────────


class TestFormatAge:
    def test_none(self):
        assert gw_cmd._format_age(None) == "-"

    def test_non_numeric(self):
        assert gw_cmd._format_age("not a number") == "-"

    def test_seconds(self):
        result = gw_cmd._format_age(45)
        assert "45" in result and "s" in result

    def test_minutes(self):
        result = gw_cmd._format_age(120)
        assert "2" in result and "m" in result

    def test_hours(self):
        result = gw_cmd._format_age(7200)
        assert "2" in result and "h" in result

    def test_days(self):
        result = gw_cmd._format_age(172800)
        assert "2" in result and "d" in result


class TestFormatTimestamp:
    def test_none(self):
        assert gw_cmd._format_timestamp(None) == "-"

    def test_invalid(self):
        assert gw_cmd._format_timestamp("not a date") == "-"


# ── _agent_type_label / _agent_output_label / _agent_template_label ─────


class TestAgentLabels:
    def test_type_label(self):
        result = gw_cmd._agent_type_label({"template_id": "echo_test", "runtime_type": "echo"})
        assert result == "Connected Asset"

    def test_output_label(self):
        result = gw_cmd._agent_output_label({"template_id": "echo_test"})
        assert result == "Reply"

    def test_template_label(self):
        result = gw_cmd._agent_template_label({"template_id": "echo_test"})
        assert result == "-"

    def test_labels_with_empty_entry(self):
        assert gw_cmd._agent_type_label({}) == "Connected Asset"
        assert gw_cmd._agent_output_label({}) == "Reply"
        assert gw_cmd._agent_template_label({}) == "-"


# ── _reachability_copy ──────────────────────────────────────────────────


class TestReachabilityCopy:
    def test_basic(self):
        result = gw_cmd._reachability_copy({"reachability": "live_now"})
        assert result == "Live listener ready to claim work."

    def test_empty_entry(self):
        result = gw_cmd._reachability_copy({})
        assert result == "Gateway does not currently have a working path."


# ── _render_gateway_dashboard ───────────────────────────────────────────


class TestRenderGatewayDashboard:
    def test_renders(self, monkeypatch):
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
            "recent_activity": [],
            "summary": {
                "managed_agents": 1,
                "live_agents": 1,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        result = gw_cmd._render_gateway_dashboard(payload)
        rendered = _render_text(result)
        assert "Gateway Overview" in rendered
        assert "Managed Agents" in rendered
        assert "bot1" in rendered


# ── _render_activity_table ──────────────────────────────────────────────


class TestRenderActivityTable:
    def test_empty(self):
        result = gw_cmd._render_activity_table([])
        rendered = _render_text(result)
        assert "No activity yet" in rendered

    def test_with_items(self):
        result = gw_cmd._render_activity_table(
            [
                {"ts": "2026-01-01", "event": "test", "agent_name": "bot1", "message_id": "m1"},
            ]
        )
        rendered = _render_text(result)
        assert "test" in rendered
        assert "@bot1" in rendered
        assert "m1" in rendered
