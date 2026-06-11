"""Shared fixtures/helpers for the per-module gateway command tests (#28 Phase 1 rewrite).

Helper monkeypatches are routed to the per-concern module each helper's callers exercise.
"""

from __future__ import annotations

import io
import json
import re
from unittest.mock import MagicMock

import httpx
from rich.console import Console
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_auth as _gw_auth
from ax_cli.commands import gateway_diagnostics as _gw_diagnostics
from ax_cli.commands import gateway_session as _gw_session
from ax_cli.commands import gateway_spaces as _gw_spaces

runner = CliRunner()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


_GOOD_SPACE_UUID = "49afd277-78d2-4a32-9858-3594cda684af"


def _strip_ansi(text: str) -> str:
    cleaned = ANSI_RE.sub("", text)
    # Rich panels wrap long messages across lines with box-drawing chars;
    # strip borders and collapse whitespace so substring assertions survive.
    cleaned = re.sub(r"[│╭╮╰╯─]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def _collapse_rich(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


class _FakeTokenExchanger:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.token = token

    def get_token(self, *args, **kwargs):
        return "jwt-test"


class _FakeLoginClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs["base_url"]
        self.token = kwargs["token"]

    def whoami(self):
        return {"username": "madtank", "email": "madtank@example.com"}

    def list_spaces(self):
        return {"spaces": [{"id": "space-1", "name": "Workspace", "is_default": True}]}


class _FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self.payload


class _FakeUserClient:
    def update_agent(self, *args, **kwargs):
        return {"ok": True}

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None, **kwargs):
        return {
            "id": "user-msg-1",
            "space_id": space_id,
            "content": content,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "metadata": metadata or {},
        }


def _seed_local_session_for_challenge(tmp_path, monkeypatch):
    """Set up a minimal approved managed agent + active local session."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "operator",
        }
    )
    token_file = tmp_path / "challenge-agent.token"
    token_file.write_text("axp_a_test.secret")
    entry = {
        "name": "challenge-agent",
        "agent_id": "agent-challenge",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "token_file": str(token_file),
        "approval_state": "approved",
        "attestation_state": "verified",
    }
    registry = {"agents": [entry]}
    session_payload = gateway_core.issue_local_session(registry, entry)
    registry = {"agents": [entry], "local_sessions": registry["local_sessions"]}
    gateway_core.save_gateway_registry(registry)

    class _SilentSendClient:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, space_id, content, **_kwargs):
            return {"message": {"id": "msg-sent-1", "space_id": space_id, "content": content}}

    monkeypatch.setattr(_gw_auth, "AxClient", _SilentSendClient)
    monkeypatch.setattr(_gw_session, "_load_managed_agent_client", lambda entry: _SilentSendClient())
    return session_payload["session_token"]


def _fake_create_agent_in_space(*args, **kwargs):
    name = kwargs.get("name", "agent")
    return {"id": f"agent-{name}", "name": name}


class _FakeSseResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self.closed = True

    def iter_lines(self):
        yield "event: connected"
        yield "data: {}"
        yield ""
        yield "event: message"
        yield f"data: {json.dumps(self.payload)}"
        yield ""


class _SharedRuntimeClient:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []
        self.processing = []
        self.tool_calls = []
        self.heartbeats = []
        self.connect_calls = 0

    def connect_sse(self, *, space_id, timeout=None):
        self.connect_calls += 1
        if self.connect_calls > 1:
            raise ConnectionError("test done")
        return _FakeSseResponse(self.payload)

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, **kwargs):
        self.sent.append(
            {
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": kwargs.get("metadata"),
                "message_type": kwargs.get("message_type", "text"),
            }
        )
        return {"message": {"id": "reply-1"}}

    def set_agent_processing_status(self, message_id, status, *, agent_name=None, space_id=None, **kwargs):
        payload = {
            "message_id": message_id,
            "status": status,
            "agent_name": agent_name,
            "space_id": space_id,
        }
        payload.update(kwargs)
        self.processing.append(payload)
        return {"ok": True}

    def record_tool_call(self, **payload):
        self.tool_calls.append(payload)
        return {"ok": True, "tool_call_id": payload["tool_call_id"]}

    def send_heartbeat(self, *, agent_id=None, status=None, note=None, cadence_seconds=None):
        self.heartbeats.append({"agent_id": agent_id, "status": status})
        return {"ok": True}

    def close(self):
        return None


class _FakeManagedSendClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs["base_url"]
        self.token = kwargs["token"]
        self.agent_name = kwargs.get("agent_name")
        self.agent_id = kwargs.get("agent_id")
        self.list_messages_calls: list[dict] = []

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
        return {
            "message": {
                "id": "msg-sent-1",
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": metadata,
            }
        }

    def list_messages(
        self,
        *,
        limit=20,
        channel="main",
        space_id=None,
        agent_id=None,
        unread_only=False,
        mark_read=False,
    ):
        self.list_messages_calls.append(
            {
                "limit": limit,
                "channel": channel,
                "space_id": space_id,
                "agent_id": agent_id,
                "unread_only": unread_only,
                "mark_read": mark_read,
            }
        )
        return {
            "messages": [
                {
                    "id": "msg-1",
                    "created_at": "2026-05-06T10:00:00Z",
                    "display_name": "operator",
                    "content": "first inbound",
                },
                {
                    "id": "msg-2",
                    "created_at": "2026-05-06T10:01:00Z",
                    "display_name": "@nemotron",
                    "content": "second inbound",
                },
            ],
            "unread_count": 2,
            "marked_read_count": 2 if mark_read else 0,
        }


def _seed_revertable_mover(tmp_path, monkeypatch):
    """Set up an agent in space-1 plus a fake user client that allows moves."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "operator",
        }
    )
    token_file = tmp_path / "mover.token"
    token_file.write_text("axp_a_mover.secret")
    allowed_spaces = [
        {"space_id": "space-1", "name": "Old Space", "is_default": True},
        {"space_id": "space-2", "name": "New Space", "is_default": False},
    ]
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "mover",
            "agent_id": "agent-mover",
            "space_id": "space-1",
            "active_space_name": "Old Space",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": allowed_spaces,
            "token_file": str(token_file),
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )
    gateway_core.save_gateway_registry(registry)

    class _FakeMover:
        def __init__(self):
            self.space_id = "space-1"
            self.calls = []

        def set_agent_placement(self, identifier, *, space_id, pinned=False):
            self.calls.append({"identifier": identifier, "space_id": space_id})
            self.space_id = space_id
            return {"agent_id": identifier, "space_id": space_id}

        def get_agent_placement(self, identifier):
            return {"agent_id": identifier, "name": "mover", "space_id": self.space_id}

        def get_agent(self, identifier):
            return {"agent": {"id": identifier, "name": "mover", "space_id": self.space_id}}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "space-1", "name": "Old Space", "slug": "old-space"},
                    {"id": "space-2", "name": "New Space", "slug": "new-space"},
                ]
            }

    fake = _FakeMover()
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: fake)
    return fake


def _seed_managed_inbox_agent(tmp_path, monkeypatch, *, agent_name="cli_god"):
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
    token_file = tmp_path / f"{agent_name}.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": agent_name,
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)


class _RecordingHeartbeatClient:
    """Records send_heartbeat / delete_agent calls for assertion."""

    def __init__(self, *, fail_with: Exception | None = None, fail_status_code: int | None = None):
        self.heartbeats: list[dict] = []
        self.deletes: list[str] = []
        self._fail_with = fail_with
        self._fail_status_code = fail_status_code

    def send_heartbeat(self, *, agent_id=None, status=None, note=None, cadence_seconds=None):
        self.heartbeats.append(
            {
                "agent_id": agent_id,
                "status": status,
                "note": note,
                "cadence_seconds": cadence_seconds,
            }
        )
        if self._fail_with is not None:
            exc = self._fail_with
            if self._fail_status_code is not None:
                resp = type("R", (), {"status_code": self._fail_status_code})()
                setattr(exc, "response", resp)
            raise exc
        return {"ok": True}

    def delete_agent(self, identifier):
        self.deletes.append(identifier)
        if self._fail_with is not None:
            raise self._fail_with
        return {"ok": True, "id": identifier}


def _stale_hermes_entry(
    name: str, *, age_seconds: float, liveness: str = "stale", agent_id: str = "agent-stale-1"
) -> dict:
    return {
        "name": name,
        "agent_id": agent_id,
        "space_id": "space-test",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "effective_state": "running",
        "liveness": liveness,
        "last_seen_age_seconds": age_seconds,
        "last_seen_at": gateway_core._now_iso(),
    }


def _build_daemon(client) -> gateway_core.GatewayDaemon:
    return gateway_core.GatewayDaemon(
        client_factory=lambda **_: client,
        poll_interval=0.0,
    )


def _isolate_gateway_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))


def _make_429_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://paxai.app/api/v1/agents")
    response = httpx.Response(429, headers={"retry-after": "12"}, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def _wire_gateway_spaces_use(monkeypatch, tmp_path):
    """Stand up a gateway session + stub resolution so `gateway spaces use`
    runs offline. Returns the captured save_space_id call dict."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-old"}
    )
    from unittest.mock import MagicMock

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: MagicMock())
    monkeypatch.setattr(_gw_spaces, "resolve_space_id", lambda c, explicit=None: "space-new")
    monkeypatch.setattr(_gw_spaces, "_space_name_for_id", lambda c, sid: "New Space")
    monkeypatch.setattr(gateway_core, "active_gateway_pid", lambda: None)
    captured = {}
    # save_space_id is lazily imported from ..config inside the command.
    monkeypatch.setattr("ax_cli.config.save_space_id", lambda sid, **kw: captured.update(sid=sid, **kw))
    return captured


def _strip(text: str) -> str:
    return ANSI_RE.sub("", text)


def _render_text(renderable) -> str:
    console = Console(record=True, width=140, color_system=None)
    console.print(renderable)
    return _strip(console.export_text())


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


def _seed_offline_gateway(tmp_path, monkeypatch):
    """Write a minimal offline gateway session and agent token for channel setup."""
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session(
        {
            "token": "offline",
            "base_url": "http://localhost:8765",
            "space_id": "00000000-0000-0000-0000-000000000001",
        }
    )
    agent_dir = tmp_path / "ax_config" / "gateway" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "token").write_text("axp_a_offline_abc123")
    registry = {
        "agents": [
            {
                "name": "my-agent",
                "agent_id": "agent-offline-1",
                "space_id": "00000000-0000-0000-0000-000000000001",
                "base_url": "http://localhost:8765",
                "token_file": str(agent_dir / "token"),
            }
        ]
    }
    gateway_core.save_gateway_registry(registry)


def _make_registry(tmp_path, *, name, runtime_type, token_text="axp_a_offline_tok"):
    config_dir = tmp_path / "ax_config"
    token_file = config_dir / "gateway" / "agents" / name / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token_text)
    return {
        "name": name,
        "agent_id": f"agent-{name}",
        "space_id": "space-1",
        "base_url": "http://localhost:8765",
        "runtime_type": runtime_type,
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
    }


def _seed_real_session(tmp_path, monkeypatch) -> None:
    """Write a real-looking gateway session file under a temp config dir."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_real.token",
            "base_url": "https://paxai.app",
            "space_id": "space-real-1",
        }
    )


def _seed_running_daemon_status(monkeypatch, *, offline: bool):
    """Stub out the heavy daemon_status / registry calls for a focused
    status-rendering test."""
    daemon_payload = {
        "running": True,
        "pid": 99999,
        "registry": {"agents": [], "gateway": {}},
    }
    ui_payload = {
        "running": True,
        "pid": 99998,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "log_path": "/tmp/ui.log",
    }
    monkeypatch.setattr(_gw_diagnostics, "daemon_status", lambda: daemon_payload)
    monkeypatch.setattr(_gw_diagnostics, "ui_status", lambda: ui_payload)
    monkeypatch.setattr(_gw_diagnostics, "list_gateway_approvals", lambda: [])
    monkeypatch.setattr(_gw_diagnostics, "load_recent_gateway_activity", lambda *a, **kw: [])
    monkeypatch.setattr(_gw_diagnostics, "_is_offline_mode_active", lambda: offline)
