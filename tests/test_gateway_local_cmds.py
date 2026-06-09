"""Per-module gateway command tests: gateway_local (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_local."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
import typer
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_local as _gw_local
from ax_cli.main import app
from tests.gateway_cmd_testlib import _collapse_rich, _FakeHttpResponse, _seed_managed_inbox_agent, _strip

runner = CliRunner()


def test_gateway_local_init_writes_tokenless_config(monkeypatch, tmp_path):
    calls = {}

    def fake_request_local_connect(**kwargs):
        calls.update(kwargs)
        return {"status": "approved", "session_token": "local-session", "agent": {"name": kwargs["agent_name"]}}

    monkeypatch.setattr(_gw_local, "_request_local_connect", fake_request_local_connect)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "init",
            "mac_backend",
            "--workdir",
            str(tmp_path),
            "--force",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    config_path = tmp_path / ".ax" / "config.toml"
    assert config_path.exists()
    config_text = config_path.read_text()
    assert 'mode = "local"' in config_text
    assert 'agent_name = "mac_backend"' in config_text
    assert "token" not in config_text
    assert "space_id" not in config_text
    assert calls["agent_name"] == "mac_backend"
    assert calls["space_id"] is None
    assert json.loads(result.output)["token_stored"] is False


def test_gateway_local_init_rejects_missing_workdir_by_default(monkeypatch, tmp_path):
    """Default behavior: --workdir must already exist; bail rather than silently mkdir."""
    monkeypatch.setattr(
        _gw_local,
        "_request_local_connect",
        lambda **kwargs: pytest.fail("connect must not run when workdir is rejected"),
    )
    missing = tmp_path / "agents" / "mac_backend"
    assert not missing.exists()

    result = runner.invoke(
        app,
        ["gateway", "local", "init", "mac_backend", "--workdir", str(missing)],
    )

    # Rich/Typer can split flag names with ANSI color escapes on color-capable
    # terminals (CI), so normalize before substring asserts.
    output = _collapse_rich(result.output)
    assert result.exit_code != 0
    assert "does not exist" in output
    assert "--create-workdir" in output
    assert not missing.exists(), "workdir must not be created without --create-workdir"
    assert not (missing / ".ax").exists()


def test_gateway_local_init_with_create_workdir_provisions_directory(monkeypatch, tmp_path):
    """`--create-workdir` opts in to making the missing folder."""
    calls = {}
    monkeypatch.setattr(
        _gw_local,
        "_request_local_connect",
        lambda **kwargs: (
            calls.setdefault("connect", kwargs)
            or {"status": "approved", "session_token": "tok", "agent": {"name": kwargs["agent_name"]}}
        ),
    )

    new_workdir = tmp_path / "agents" / "fresh"
    assert not new_workdir.exists()

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "init",
            "fresh",
            "--workdir",
            str(new_workdir),
            "--create-workdir",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert new_workdir.is_dir()
    assert (new_workdir / ".ax" / "config.toml").exists()


def test_gateway_local_init_rejects_workdir_pointing_at_a_file(monkeypatch, tmp_path):
    """If --workdir resolves to an existing file, fail with a clear error."""
    monkeypatch.setattr(
        _gw_local,
        "_request_local_connect",
        lambda **kwargs: pytest.fail("connect must not run when workdir is invalid"),
    )
    file_path = tmp_path / "not-a-dir.txt"
    file_path.write_text("nope")

    result = runner.invoke(
        app,
        ["gateway", "local", "init", "x", "--workdir", str(file_path)],
    )

    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_ensure_workdir_helper_no_create_when_exists(tmp_path):
    """The helper is a no-op when the workdir already exists as a directory."""
    existing = tmp_path / "already_here"
    existing.mkdir()
    # Should not raise; should not modify anything observable.
    _gw_local._ensure_workdir(existing, create=False)
    assert existing.is_dir()


def test_gateway_local_send_pending_approval_guides_agent(monkeypatch):
    monkeypatch.setattr(
        _gw_local,
        "_request_local_connect",
        lambda **kwargs: {
            "status": "pending",
            "approval_id": "approval-456",
            "agent": {
                "name": "backend_sentinel",
                "workdir": "/Users/jacob/claude_home/ax-backend-extract",
                "active_space_name": "madtank's Workspace",
            },
            "approval": {"approval_kind": "new_binding", "risk": "medium"},
        },
    )

    with pytest.raises(ValueError) as exc:
        _gw_local._resolve_local_gateway_session(
            session_token=None,
            agent_name="backend_sentinel",
            gateway_url="http://127.0.0.1:8765",
            workdir="/Users/jacob/claude_home/ax-backend-extract",
        )

    message = str(exc.value)
    assert "Gateway approval required for @backend_sentinel" in message
    assert "open http://127.0.0.1:8765" in message.lower()
    assert "approval_id=approval-456" in message
    assert "workdir=/Users/jacob/claude_home/ax-backend-extract" in message
    assert "Do not fall back to a direct PAT" in message


def test_gateway_local_connect_infers_agent_from_workdir_config(monkeypatch, tmp_path):
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    (ax_dir / "config.toml").write_text(
        '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n[agent]\nagent_name = "frontend_sentinel"\n'
    )

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "pending", "approval_id": "approval-frontend"}

    def fake_post(url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(
        _gw_local,
        "_local_process_fingerprint",
        lambda **kwargs: {"agent_name": kwargs["agent_name"], "cwd": kwargs["cwd"]},
    )

    payload = _gw_local._request_local_connect(workdir=str(tmp_path))

    assert payload["approval_id"] == "approval-frontend"
    assert captured["json"]["agent_name"] == "frontend_sentinel"
    assert captured["json"]["fingerprint"] == {"agent_name": "frontend_sentinel", "cwd": str(tmp_path)}


def test_gateway_local_connect_infers_agent_from_cwd_config(monkeypatch, tmp_path):
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    (ax_dir / "config.toml").write_text(
        '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n[agent]\nagent_name = "frontend_sentinel"\n'
    )

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "pending", "approval_id": "approval-frontend"}

    def fake_post(url, *, json=None, timeout=None):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(
        _gw_local,
        "_local_process_fingerprint",
        lambda **kwargs: {"agent_name": kwargs["agent_name"], "cwd": kwargs["cwd"]},
    )

    payload = _gw_local._request_local_connect()

    assert payload["approval_id"] == "approval-frontend"
    assert captured["json"]["agent_name"] == "frontend_sentinel"
    assert captured["json"]["fingerprint"] == {"agent_name": "frontend_sentinel", "cwd": str(tmp_path)}


def test_gateway_local_connect_rejects_agent_workdir_mismatch(tmp_path):
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    (ax_dir / "config.toml").write_text('[gateway]\nmode = "local"\n[agent]\nagent_name = "frontend_sentinel"\n')

    with pytest.raises(ValueError) as exc:
        _gw_local._request_local_connect(agent_name="codex", workdir=str(tmp_path))

    assert "Gateway identity mismatch" in str(exc.value)
    assert "configured for @frontend_sentinel" in str(exc.value)
    assert "requested @codex" in str(exc.value)


def test_gateway_local_send_auto_connects_with_agent(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if url.endswith("/local/connect"):
            return _FakeHttpResponse(
                {
                    "status": "approved",
                    "agent": {"name": "codex-pass-through"},
                    "registry_ref": "#4",
                    "session_token": "axgw_s_test.session",
                }
            )
        if url.endswith("/local/send"):
            return _FakeHttpResponse(
                {
                    "agent": "codex-pass-through",
                    "message": {"id": "msg-1", "content": json["content"], "space_id": json.get("space_id")},
                },
                status_code=201,
            )
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"method": "GET", "url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse(
            {
                "agent": "codex-pass-through",
                "messages": [{"id": "reply-1", "content": "@codex-pass-through received"}],
                "marked_read_count": 1,
            }
        )

    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(_gw_local.httpx, "get", fake_get)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "send",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "@night-owl please QA PR 114",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    connects = [c for c in calls if c.get("url", "").endswith("/local/connect")]
    sends = [c for c in calls if c.get("url", "").endswith("/local/send")]
    inbox_gets = [c for c in calls if c.get("method") == "GET" and c.get("url", "").endswith("/local/inbox")]
    assert connects and connects[0]["json"]["agent_name"] == "codex-pass-through"
    assert len(sends) == 1
    assert sends[0]["headers"] == {"X-Gateway-Session": "axgw_s_test.session"}
    assert sends[0]["json"]["content"] == "@night-owl please QA PR 114"
    # Pre-send pending-reply check must NOT mark messages read; post-send inbox poll must.
    pre_send = [g for g in inbox_gets if g["params"].get("mark_read") == "false"]
    post_send = [g for g in inbox_gets if g["params"].get("mark_read") == "true"]
    assert pre_send, "expected pre-send pending-reply check via /local/inbox with mark_read=false"
    assert post_send, "expected post-send inbox poll via /local/inbox with mark_read=true"
    payload = json.loads(result.output)
    assert payload["agent"] == "codex-pass-through"
    assert payload["connect"]["agent"] == "codex-pass-through"
    assert payload["inbox"]["messages"][0]["content"] == "@codex-pass-through received"
    # Pending-reply receipt fields are present (zero in this test since fake_get returns one msg only after send).
    assert "pending_reply_count" in payload
    assert "pending_reply_message_ids" in payload
    assert "pending_reply_newest_senders" in payload


def test_gateway_local_send_can_skip_inbox_check(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "timeout": timeout})
        if url.endswith("/local/connect"):
            return _FakeHttpResponse(
                {
                    "status": "approved",
                    "agent": {"name": "codex-pass-through"},
                    "registry_ref": "#4",
                    "session_token": "axgw_s_test.session",
                }
            )
        if url.endswith("/local/send"):
            return _FakeHttpResponse({"agent": "codex-pass-through", "message": {"id": "msg-1"}}, status_code=201)
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, params=None, headers=None, timeout=None):
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(_gw_local.httpx, "get", fake_get)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "send",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "--no-inbox",
            "@night-owl please QA PR 114",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call["method"] for call in calls] == ["POST", "POST"]
    payload = json.loads(result.output)
    assert "inbox" not in payload


def test_gateway_local_inbox_auto_connects_and_marks_read(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse(
            {
                "status": "approved",
                "agent": {"name": "codex-pass-through"},
                "registry_ref": "#4",
                "session_token": "axgw_s_test.session",
            }
        )

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"method": "GET", "url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse({"agent": "codex-pass-through", "messages": [], "count": 0})

    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(_gw_local.httpx, "get", fake_get)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "inbox",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[1]["method"] == "GET"
    assert calls[1]["headers"] == {"X-Gateway-Session": "axgw_s_test.session"}
    assert calls[1]["params"]["mark_read"] == "true"
    payload = json.loads(result.output)
    assert payload["agent"] == "codex-pass-through"
    assert payload["connect"]["registry_ref"] == "#4"


def test_gateway_local_inbox_waits_until_message_arrives(monkeypatch):
    calls = []
    get_count = {"value": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse(
            {
                "status": "approved",
                "agent": {"name": "codex-pass-through"},
                "registry_ref": "#4",
                "session_token": "axgw_s_test.session",
            }
        )

    def fake_get(url, params=None, headers=None, timeout=None):
        get_count["value"] += 1
        calls.append({"method": "GET", "url": url, "params": params, "headers": headers, "timeout": timeout})
        messages = [] if get_count["value"] == 1 else [{"id": "msg-1", "content": "ready"}]
        return _FakeHttpResponse({"agent": "codex-pass-through", "messages": messages, "count": len(messages)})

    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(_gw_local.httpx, "get", fake_get)
    monkeypatch.setattr(_gw_local.time, "sleep", lambda _seconds: None)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "inbox",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "--wait",
            "3",
            "--poll-interval",
            "0.5",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert get_count["value"] == 2
    payload = json.loads(result.output)
    assert payload["messages"] == [{"id": "msg-1", "content": "ready"}]
    assert payload["waited_seconds"] == 3


def test_local_send_resolves_slug_before_proxying(monkeypatch, tmp_path):
    """`ax gateway local send --space <slug>` resolves through the cache and
    forwards a UUID to the daemon, so the upstream API never sees the slug."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache(
        [{"id": "12345678-1234-4234-8234-123456789012", "name": "ax-cli-dev", "slug": "ax-cli-dev"}]
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeHttpResponse({"agent": "codex-pass-through", "message": {"id": "msg-1"}})

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeHttpResponse({"agent": "codex-pass-through", "messages": [], "count": 0})

    def fake_resolve_session(**kwargs):
        captured["session_space_id"] = kwargs.get("space_id")
        return ("axgw_s_test.session", {"status": "approved"})

    monkeypatch.setattr(_gw_local, "_resolve_local_gateway_session", fake_resolve_session)
    monkeypatch.setattr(_gw_local, "_check_local_pending_replies", lambda **_: {"count": 0, "message_ids": []})
    monkeypatch.setattr(_gw_local.httpx, "post", fake_post)
    monkeypatch.setattr(_gw_local.httpx, "get", fake_get)

    result = runner.invoke(
        app,
        ["gateway", "local", "send", "hello", "--space", "ax-cli-dev", "--no-inbox", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert captured["json"]["space_id"] == "12345678-1234-4234-8234-123456789012"
    assert captured["session_space_id"] == "12345678-1234-4234-8234-123456789012"


@pytest.mark.parametrize(
    ("argv", "needs_managed_seed", "needs_no_inbox_hint"),
    [
        (["gateway", "local", "send", "hello", "--space", "never-seen", "--no-inbox"], False, True),
        (["gateway", "agents", "inbox", "cli_god", "--space", "never-seen"], True, False),
    ],
)
def test_unknown_slug_errors_clearly(monkeypatch, tmp_path, argv, needs_managed_seed, needs_no_inbox_hint):
    if needs_managed_seed:
        _seed_managed_inbox_agent(tmp_path, monkeypatch)

    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache([])

    monkeypatch.setattr(
        _gw_local,
        "_resolve_local_gateway_session",
        lambda **kwargs: pytest.fail("session must not be opened when slug fails to resolve"),
    )

    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    assert "Could not resolve space" in result.output
    if needs_no_inbox_hint:
        assert "ax spaces list" in result.output


def test_local_inbox_resolves_slug_before_proxying(monkeypatch, tmp_path):
    """Same slug → UUID resolution applies to ax gateway local inbox."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache(
        [{"id": "12345678-1234-4234-8234-123456789012", "name": "ax-cli-dev", "slug": "ax-cli-dev"}]
    )
    captured = {}

    def fake_resolve_session(**kwargs):
        captured["session_space_id"] = kwargs.get("space_id")
        return ("axgw_s_test.session", None)

    def fake_poll(**kwargs):
        captured["poll_space_id"] = kwargs.get("space_id")
        return {"agent": "codex-pass-through", "messages": []}

    monkeypatch.setattr(_gw_local, "_resolve_local_gateway_session", fake_resolve_session)
    monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", fake_poll)

    result = runner.invoke(
        app,
        ["gateway", "local", "inbox", "--space", "ax-cli-dev", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert captured["session_space_id"] == "12345678-1234-4234-8234-123456789012"
    assert captured["poll_space_id"] == "12345678-1234-4234-8234-123456789012"


def test_local_route_failure_guidance_404_suggests_recovery():
    msg = _gw_local._local_route_failure_guidance(
        detail="not found",
        status_code=404,
        gateway_url="http://127.0.0.1:8765",
        agent_name="wishy",
        workdir="/repo",
        action="local connect",
    )
    assert "Gateway local connect failed for @wishy: not found" in msg
    # The whole point of this PR — the message must point at the Live Listener
    # case so users running `ax auth whoami` in a claude_code_channel workspace
    # don't get a bare "not found".
    assert "Live Listener" in msg
    assert "claude_code_channel" in msg
    assert "ax gateway agents list --json" in msg
    assert "ax gateway local connect wishy --workdir /repo" in msg
    assert "http://127.0.0.1:8765" in msg


def test_local_route_failure_guidance_non_404_stays_terse():
    msg = _gw_local._local_route_failure_guidance(
        detail="connection refused",
        status_code=None,
        gateway_url="http://127.0.0.1:8765/",
        agent_name=None,
        workdir=None,
        action="proxy whoami",
    )
    assert "Gateway proxy whoami failed for this workspace: connection refused" in msg
    assert "Live Listener" not in msg  # only suggested for 404s
    assert "Or open http://127.0.0.1:8765 to inspect Gateway agents." in msg


class TestGatewayLocalConfigText:
    def test_basic_output(self):
        text = _gw_local._gateway_local_config_text(
            agent_name="test-bot",
            gateway_url="http://127.0.0.1:8765",
        )
        assert 'mode = "local"' in text
        assert 'agent_name = "test-bot"' in text
        assert 'url = "http://127.0.0.1:8765"' in text

    def test_with_workdir(self):
        text = _gw_local._gateway_local_config_text(
            agent_name="bot",
            gateway_url="http://localhost:8765",
            workdir="/tmp/w",
        )
        assert 'workdir = "/tmp/w"' in text

    def test_without_workdir(self):
        text = _gw_local._gateway_local_config_text(
            agent_name="bot",
            gateway_url="http://localhost:8765",
        )
        assert "workdir" not in text


class TestGatewayLocalConfigFromWorkdir:
    def test_none_workdir(self):
        assert _gw_local._gateway_local_config_from_workdir(None) == {}

    def test_empty_workdir(self):
        assert _gw_local._gateway_local_config_from_workdir("") == {}

    def test_missing_config(self, tmp_path):
        assert _gw_local._gateway_local_config_from_workdir(str(tmp_path)) == {}

    def test_valid_config(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "mybot"\nregistry_ref = "ref-1"\n'
        )
        result = _gw_local._gateway_local_config_from_workdir(str(tmp_path))
        assert result["agent_name"] == "mybot"
        assert result["registry_ref"] == "ref-1"
        assert result["gateway_url"] == "http://127.0.0.1:8765"

    def test_non_local_mode_without_url_returns_empty(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text('[agent]\nagent_name = "mybot"\n')
        result = _gw_local._gateway_local_config_from_workdir(str(tmp_path))
        assert result == {}

    def test_invalid_toml(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text("not valid [[ toml {{{")
        result = _gw_local._gateway_local_config_from_workdir(str(tmp_path))
        assert result == {}


class TestResolveLocalGatewayIdentity:
    def test_agent_name_passthrough(self, tmp_path):
        name, ref = _gw_local._resolve_local_gateway_identity(
            agent_name="bot", registry_ref=None, workdir=str(tmp_path)
        )
        assert name == "bot"
        assert ref is None

    def test_registry_ref_passthrough(self, tmp_path):
        name, ref = _gw_local._resolve_local_gateway_identity(
            agent_name=None, registry_ref="ref-1", workdir=str(tmp_path)
        )
        assert name is None
        assert ref == "ref-1"

    def test_reads_from_workdir_config(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://localhost:8765"\n\n[agent]\nagent_name = "configured-bot"\n'
        )
        name, ref = _gw_local._resolve_local_gateway_identity(agent_name=None, registry_ref=None, workdir=str(tmp_path))
        assert name == "configured-bot"

    def test_mismatch_raises(self, tmp_path):
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://localhost:8765"\n\n[agent]\nagent_name = "configured-bot"\n'
        )
        with pytest.raises(ValueError, match="identity mismatch"):
            _gw_local._resolve_local_gateway_identity(
                agent_name="different-bot", registry_ref=None, workdir=str(tmp_path)
            )


class TestLocalRouteFailureGuidance:
    def test_404_includes_suggestions(self):
        msg = _gw_local._local_route_failure_guidance(
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
        msg = _gw_local._local_route_failure_guidance(
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
        msg = _gw_local._local_route_failure_guidance(
            detail="err",
            status_code=None,
            gateway_url="http://127.0.0.1:8765",
            agent_name=None,
            workdir=None,
            action="proxy",
        )
        assert "this workspace" in msg


class TestApprovalRequiredGuidance:
    def test_basic_guidance(self):
        msg = _gw_local._approval_required_guidance(
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
        msg = _gw_local._approval_required_guidance(
            connect_payload={"status": "pending"},
            gateway_url="http://127.0.0.1:8765",
        )
        assert "approval required" in msg.lower()


class TestEnsureWorkdir:
    def test_existing_dir_ok(self, tmp_path):
        _gw_local._ensure_workdir(tmp_path, create=False)

    def test_file_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        with pytest.raises(typer.BadParameter, match="not a directory"):
            _gw_local._ensure_workdir(f, create=False)

    def test_missing_no_create_raises(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(typer.BadParameter, match="does not exist"):
            _gw_local._ensure_workdir(missing, create=False)

    def test_create_makes_dir(self, tmp_path):
        target = tmp_path / "new" / "deep"
        _gw_local._ensure_workdir(target, create=True)
        assert target.is_dir()


class TestPrintPendingReplyWarningLocal:
    def test_no_warning_on_zero(self, capsys):
        _gw_local._print_pending_reply_warning_local({"count": 0})
        # nothing printed to stdout
        assert capsys.readouterr().out == ""

    def test_no_warning_on_non_dict(self, capsys):
        _gw_local._print_pending_reply_warning_local("not a dict")
        assert capsys.readouterr().out == ""


class TestCheckLocalPendingReplies:
    def test_returns_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local, "_poll_local_inbox_over_http", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        result = _gw_local._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_returns_empty_on_non_dict(self, monkeypatch):
        monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", lambda **kw: "not-dict")
        result = _gw_local._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_returns_empty_when_no_messages(self, monkeypatch):
        monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", lambda **kw: {"messages": []})
        result = _gw_local._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 0

    def test_extracts_senders_and_ids(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_poll_local_inbox_over_http",
            lambda **kw: {
                "messages": [
                    {"id": "m1", "display_name": "alice"},
                    {"id": "m2", "agent_name": "bob"},
                ],
                "unread_count": 2,
            },
        )
        result = _gw_local._check_local_pending_replies(
            gateway_url="http://127.0.0.1:8765",
            session_token="tok",
        )
        assert result["count"] == 2
        assert result["message_ids"] == ["m1", "m2"]
        assert result["newest_senders"] == ["alice", "bob"]


class TestLocalConnectCommand:
    def test_json(self, monkeypatch):
        payload = {"status": "approved", "session_token": "tok", "agent": {"name": "bot1"}}
        monkeypatch.setattr(_gw_local, "_request_local_connect", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "local", "connect", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "approved"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_request_local_connect",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad")),
        )
        result = runner.invoke(app, ["gateway", "local", "connect", "bot1"])
        assert result.exit_code != 0


class TestLocalSendCommand:
    def test_json_with_session(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", None),
        )
        monkeypatch.setattr(
            _gw_local,
            "_check_local_pending_replies",
            lambda **kw: {"count": 0, "message_ids": [], "newest_senders": []},
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 201
        fake_resp.json.return_value = {"agent": "bot1", "message": {"id": "m1"}}
        fake_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: fake_resp)
        monkeypatch.setattr(
            _gw_local,
            "_poll_local_inbox_over_http",
            lambda **kw: {"messages": []},
        )
        monkeypatch.setattr(_gw_local, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello world", "--session-token", "tok-123", "--json", "--no-inbox"],
        )
        assert result.exit_code == 0

    def test_send_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: (_ for _ in ()).throw(ValueError("no session")),
        )
        result = runner.invoke(app, ["gateway", "local", "send", "hello", "--session-token", "bad"])
        assert result.exit_code != 0


class TestLocalInboxCommand:
    def test_json_with_session(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok", None),
        )
        monkeypatch.setattr(
            _gw_local,
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


class TestResolveLocalGatewaySession:
    def test_returns_existing_token(self):
        token, payload = _gw_local._resolve_local_gateway_session(session_token="my-token")
        assert token == "my-token"
        assert payload is None

    def test_connects_when_no_token(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_request_local_connect",
            lambda **kw: {"session_token": "new-tok", "status": "approved"},
        )
        token, payload = _gw_local._resolve_local_gateway_session(session_token=None, agent_name="bot1")
        assert token == "new-tok"
        assert payload["status"] == "approved"

    def test_raises_on_pending(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_request_local_connect",
            lambda **kw: {"status": "pending"},
        )
        with pytest.raises(ValueError, match="approval required"):
            _gw_local._resolve_local_gateway_session(session_token=None, agent_name="bot1")

    def test_raises_on_rejected(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_request_local_connect",
            lambda **kw: {"status": "rejected"},
        )
        with pytest.raises(ValueError, match="rejected"):
            _gw_local._resolve_local_gateway_session(session_token=None, agent_name="bot1")


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


class TestLocalSendTextOutput:
    def _setup_send_mocks(self, monkeypatch, response_json):
        monkeypatch.setattr(
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", None),
        )
        monkeypatch.setattr(
            _gw_local,
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
            _gw_local,
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
            _gw_local,
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

        monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", _fail)
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output

    def test_json_with_connect_payload(self, monkeypatch):
        monkeypatch.setattr(
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", {"status": "approved", "registry_ref": "ref-1", "agent": {"name": "bot1"}}),
        )
        monkeypatch.setattr(
            _gw_local,
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
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok-123", None),
        )
        monkeypatch.setattr(
            _gw_local,
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
        monkeypatch.setattr(_gw_local, "_resolve_space_via_cache", lambda v: None)
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
            _gw_local,
            "_check_local_pending_replies",
            lambda **kw: {"count": 2, "message_ids": ["m1", "m2"], "newest_senders": ["alice"]},
        )
        monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", lambda **kw: {"messages": []})
        result = runner.invoke(
            app,
            ["gateway", "local", "send", "hello", "--session-token", "tok-123"],
        )
        assert result.exit_code == 0


class TestLocalInboxTextOutput:
    def test_text_with_messages(self, monkeypatch):
        monkeypatch.setattr(_gw_local, "_resolve_local_gateway_session", lambda **kw: ("tok", None))
        monkeypatch.setattr(
            _gw_local,
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
            _gw_local,
            "_resolve_local_gateway_session",
            lambda **kw: ("tok", {"status": "approved", "registry_ref": "r1", "agent": {"name": "bot1"}}),
        )
        monkeypatch.setattr(
            _gw_local,
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
        monkeypatch.setattr(_gw_local, "_resolve_local_gateway_session", lambda **kw: ("tok", None))

        fake_response = MagicMock()
        fake_response.status_code = 500
        fake_response.text = "internal error"
        fake_response.json.return_value = {"error": "internal error"}

        def _raise(**kw):
            raise httpx.HTTPStatusError("bad", request=MagicMock(), response=fake_response)

        monkeypatch.setattr(_gw_local, "_poll_local_inbox_over_http", _raise)
        result = runner.invoke(app, ["gateway", "local", "inbox", "--session-token", "tok"])
        assert result.exit_code != 0

    def test_inbox_generic_error(self, monkeypatch):
        monkeypatch.setattr(_gw_local, "_resolve_local_gateway_session", lambda **kw: ("tok", None))
        monkeypatch.setattr(
            _gw_local,
            "_poll_local_inbox_over_http",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = runner.invoke(app, ["gateway", "local", "inbox", "--session-token", "tok"])
        assert result.exit_code != 0

    def test_inbox_space_resolution_fails(self, monkeypatch):
        monkeypatch.setattr(_gw_local, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "local", "inbox", "--session-token", "tok", "--space", "bad-slug"],
        )
        assert result.exit_code != 0
        assert "Could not resolve" in _strip(result.output)


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
            _gw_local,
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

        monkeypatch.setattr(_gw_local, "_request_local_connect", _raise)
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


class TestPrintPendingReplyWarningExtra:
    def test_warning_with_single_sender(self, capsys):
        _gw_local._print_pending_reply_warning_local({"count": 1, "newest_senders": ["alice"]})
        # Should have produced some output (Rich console writes to stderr by default
        # but _print_pending_reply_warning_local uses console which writes to stdout)
        # The test just verifies it does not crash

    def test_warning_with_multiple_senders(self, capsys):
        _gw_local._print_pending_reply_warning_local({"count": 3, "newest_senders": ["alice", "bob", "charlie"]})

    def test_warning_plural(self, capsys):
        _gw_local._print_pending_reply_warning_local({"count": 5, "newest_senders": []})
