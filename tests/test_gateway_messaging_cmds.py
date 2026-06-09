"""Per-module gateway command tests: gateway_messaging (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_messaging."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_messaging as _gw_messaging
from ax_cli.main import app
from tests.gateway_cmd_testlib import _FakeManagedSendClient, _make_registry, _seed_managed_inbox_agent, _strip

runner = CliRunner()


def test_gateway_agents_send_uses_managed_identity(monkeypatch, tmp_path):
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
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "sender-bot"
    assert payload["content"] == "@codex hello there"
    assert payload["message"]["metadata"]["gateway"]["sent_via"] == "gateway_cli"
    recent = gateway_core.load_recent_gateway_activity()
    # The send event must appear, but is no longer guaranteed to be last —
    # the default-on post-send inbox poll (aX task 663d9e6f) appends a
    # `managed_inbox_polled` event after it.
    assert any(item["event"] == "manual_message_sent" for item in recent)


def test_gateway_agents_send_rejects_user_bootstrap_pat(monkeypatch, tmp_path):
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
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_u_user.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex"])

    assert result.exit_code == 1, result.output
    assert "agent-bound token" in result.output
    assert "user" in result.output
    assert "bootstrap PAT" in result.output


def test_gateway_agents_send_acknowledges_pending_inbox_message(monkeypatch, tmp_path):
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
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "backlog_depth": 1,
            "current_status": "queued",
            "current_activity": "Queued in Gateway",
            "last_received_message_id": "msg-queued-1",
            "last_work_received_at": "2026-04-23T18:00:00+00:00",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.save_agent_pending_messages(
        "sender-bot",
        [
            {
                "message_id": "msg-queued-1",
                "parent_id": None,
                "conversation_id": "msg-queued-1",
                "content": "@sender-bot hello there",
                "display_name": "madtank",
                "created_at": "2026-04-23T18:00:00+00:00",
                "queued_at": "2026-04-23T18:00:01+00:00",
            }
        ],
    )
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "send",
            "sender-bot",
            "handled",
            "--parent-id",
            "msg-queued-1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["message"]["parent_id"] == "msg-queued-1"
    assert gateway_core.load_agent_pending_messages("sender-bot") == []
    updated = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "sender-bot")
    assert updated["backlog_depth"] == 0
    assert updated["current_status"] is None
    assert updated["current_activity"] is None
    assert updated["processed_count"] == 1
    assert updated["last_reply_message_id"] == "msg-sent-1"
    recent = gateway_core.load_recent_gateway_activity()
    # Same nuance as the sister test: the queue-ack event is in the recent
    # log but no longer trailing because the default-on post-send inbox
    # poll appends afterwards.
    assert any(item["event"] == "manual_queue_acknowledged" for item in recent)


def test_gateway_agents_send_blocks_identity_mismatch(monkeypatch, tmp_path):
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
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "install_id": "inst-sender-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )
    registry["identity_bindings"][0]["acting_identity"]["agent_name"] = "night_owl"
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex", "--json"])

    assert result.exit_code == 1, result.output
    assert "identity_mismatch" in result.output.lower() or "mismatched acting identity" in result.output.lower()


def test_gateway_agents_inbox_returns_messages_for_managed_agent(monkeypatch, tmp_path):
    """ax-cli-dev 70f08787: a Live Listener seat must be able to peek its own inbox
    through Gateway with no PAT exposed to the caller."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        ["gateway", "agents", "inbox", "cli_god", "--limit", "20", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "cli_god"
    assert payload["agent_id"] == "agent-1"
    assert payload["space_id"] == "space-1"
    assert payload["unread_count"] == 2
    # Default --no-mark-read so peek does not consume the agent's queue.
    assert payload["marked_read_count"] == 0
    assert [m["id"] for m in payload["messages"]] == ["msg-1", "msg-2"]
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "managed_inbox_polled"


def test_gateway_agents_inbox_human_output_prints_message_table(monkeypatch, tmp_path):
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "inbox", "cli_god"])

    assert result.exit_code == 0, result.output
    assert "@cli_god" in result.output
    assert "first inbound" in result.output
    assert "second inbound" in result.output
    assert "unread_count = 2" in result.output


def test_gateway_agents_inbox_mark_read_flag_propagates(monkeypatch, tmp_path):
    """--mark-read must reach client.list_messages so the operator can opt in
    to consuming the agent's queue when that's the explicit intent."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    captured: list[_FakeManagedSendClient] = []

    class _RecordingClient(_FakeManagedSendClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(_gw_agents, "AxClient", _RecordingClient)

    result = runner.invoke(
        app,
        ["gateway", "agents", "inbox", "cli_god", "--mark-read", "--unread-only", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert captured, "fake client was never instantiated"
    call = captured[-1].list_messages_calls[-1]
    assert call["mark_read"] is True
    assert call["unread_only"] is True
    assert call["space_id"] == "space-1"
    assert call["agent_id"] == "agent-1"


def test_gateway_agents_inbox_errors_when_agent_missing(monkeypatch, tmp_path):
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
    # Empty registry — no managed agent named "ghost".

    result = runner.invoke(app, ["gateway", "agents", "inbox", "ghost"])

    assert result.exit_code == 1
    assert "Managed agent not found" in result.output
    assert "ghost" in result.output


def test_gateway_agents_inbox_helper_invocable_from_http_route(monkeypatch, tmp_path):
    """The helper that powers the CLI must be callable in-process so the
    /api/agents/<name>/inbox HTTP route can reuse it. Smoke-test the helper
    directly to lock in that contract for the web UI / future remote callers."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    payload = _gw_messaging._inbox_for_managed_agent(name="cli_god", limit=5)

    assert payload["agent"] == "cli_god"
    assert payload["space_id"] == "space-1"
    assert len(payload["messages"]) == 2


def test_send_from_managed_agent_bundles_unread_inbox_by_default(monkeypatch, tmp_path):
    """ax-cli-dev 663d9e6f: every send-as-agent path should bundle "what arrived
    while you were drafting" so two agents don't talk past each other."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    # Seed a pending message so unread_only's intersection returns it.
    gateway_core.save_agent_pending_messages(
        "cli_god",
        [{"message_id": "msg-1", "content": "first inbound", "queued_at": "2026-05-08T00:00:00Z"}],
    )
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        ["gateway", "agents", "send", "cli_god", "thanks!", "--inbox-wait", "0", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "cli_god"
    assert payload["content"] == "thanks!"
    assert "inbox" in payload, "default-on inbox bundling missing from response"
    inbox = payload["inbox"]
    assert inbox["agent"] == "cli_god"
    assert inbox["unread_count"] == 1
    assert any(m.get("id") == "msg-1" for m in inbox["messages"])


def test_send_from_managed_agent_skips_inbox_when_disabled(monkeypatch, tmp_path):
    """`--no-inbox` opts out of the post-send poll entirely."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    gateway_core.save_agent_pending_messages(
        "cli_god",
        [{"message_id": "msg-1", "content": "first inbound", "queued_at": "2026-05-08T00:00:00Z"}],
    )
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        ["gateway", "agents", "send", "cli_god", "skip inbox", "--no-inbox", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "inbox" not in payload
    assert "inbox_error" not in payload
    # Pending queue is preserved because the post-send poll never ran.
    assert len(gateway_core.load_agent_pending_messages("cli_god")) == 1


def test_send_from_managed_agent_inbox_error_does_not_break_send(monkeypatch, tmp_path):
    """If the post-send poll raises, the send result still ships and the error
    is surfaced as inbox_error so the caller sees the partial outcome."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    def boom(**_kwargs):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(_gw_messaging, "_poll_managed_agent_inbox_after_send", boom)

    result = runner.invoke(
        app,
        ["gateway", "agents", "send", "cli_god", "even on error", "--inbox-wait", "0", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Send still succeeded.
    assert payload["agent"] == "cli_god"
    assert payload["content"] == "even on error"
    assert payload["message"]["id"] == "msg-sent-1"
    # Error path surfaces.
    assert payload.get("inbox_error") == "upstream 503"
    assert "inbox" not in payload


def test_send_from_managed_agent_inbox_returns_empty_when_no_unread(monkeypatch, tmp_path):
    """An empty inbox still returns the bundle structure with messages=[] and
    unread_count=0 so callers can rely on the field shape."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    # No pending messages seeded, so unread_only intersection -> empty list.
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        ["gateway", "agents", "send", "cli_god", "quiet send", "--inbox-wait", "0", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload.get("inbox") is not None
    assert payload["inbox"]["messages"] == []
    assert payload["inbox"]["unread_count"] == 0


def test_agents_inbox_resolves_slug_before_lookup(monkeypatch, tmp_path):
    """`ax gateway agents inbox --space <slug>` also resolves through the cache."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    gateway_core.save_space_cache([{"id": "space-1", "name": "Test Space", "slug": "test-space"}])
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)
    captured = {}

    real_inbox = _gw_messaging._inbox_for_managed_agent

    def spy_inbox(*, name, limit, channel, space_id, unread_only, mark_read):
        captured["space_id"] = space_id
        return real_inbox(
            name=name,
            limit=limit,
            channel=channel,
            space_id=space_id,
            unread_only=unread_only,
            mark_read=mark_read,
        )

    monkeypatch.setattr(_gw_messaging, "_inbox_for_managed_agent", spy_inbox)

    result = runner.invoke(app, ["gateway", "agents", "inbox", "cli_god", "--space", "test-space", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["space_id"] == "space-1"


def test_inbox_for_managed_agent_clears_pending_queue_on_mark_read(monkeypatch, tmp_path):
    """`ax gateway agents inbox <name> --mark-read` must clear the local
    pending queue so backlog_depth/queue_depth go to 0. Without this fix
    the side-app badge stuck at the old count even though the upstream
    confirmed the messages were marked read — the gbr-coordinator report.
    """
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    gateway_core.save_agent_pending_messages(
        "cli_god",
        [
            {"message_id": "m-1", "content": "first", "queued_at": "2026-05-08T00:00:00Z"},
            {"message_id": "m-2", "content": "second", "queued_at": "2026-05-08T00:01:00Z"},
            {"message_id": "m-3", "content": "third", "queued_at": "2026-05-08T00:02:00Z"},
        ],
    )
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    payload = _gw_messaging._inbox_for_managed_agent(name="cli_god", limit=10, mark_read=True)

    # Endpoint reports how many local items it cleared.
    assert payload["local_marked_read_count"] == 3
    # On-disk queue is empty.
    assert gateway_core.load_agent_pending_messages("cli_god") == []
    # Registry-side counters reflect the cleared state — that's what the
    # UI badge actually reads.
    registry_after = gateway_core.load_gateway_registry()
    stored = _gw_messaging.find_agent_entry(registry_after, "cli_god")
    assert stored["backlog_depth"] == 0
    assert stored["queue_depth"] == 0
    assert stored["current_status"] is None


def test_inbox_for_managed_agent_does_not_touch_pending_queue_without_mark_read(monkeypatch, tmp_path):
    """Plain peek (`mark_read=False`) must NOT clear the queue — operators
    inspecting on the agent's behalf shouldn't silently drain the agent's
    work."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    gateway_core.save_agent_pending_messages(
        "cli_god",
        [{"message_id": "m-1", "content": "first", "queued_at": "2026-05-08T00:00:00Z"}],
    )
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    _gw_messaging._inbox_for_managed_agent(name="cli_god", limit=10, mark_read=False)

    # Queue is preserved.
    assert len(gateway_core.load_agent_pending_messages("cli_god")) == 1


def test_inbox_for_managed_agent_unread_only_intersects_pending_queue(monkeypatch, tmp_path):
    """The drawer's `unread_only=true` request must filter the upstream
    listing to messages the local pending queue tracks. Without this, the
    upstream returns every message in the agent's view (20 by default) and
    the drawer shows "3 unread messages" header above a 20-row body —
    exactly the misalignment Jacob hit on gbr-coordinator."""
    _seed_managed_inbox_agent(tmp_path, monkeypatch)
    # Pending queue has only msg-1 — that's "unread" by Gateway's definition.
    gateway_core.save_agent_pending_messages(
        "cli_god",
        [{"message_id": "msg-1", "content": "queued", "queued_at": "2026-05-08T00:00:00Z"}],
    )

    class FakeUpstreamClient:
        def list_messages(self, *, limit, channel, space_id, agent_id, unread_only, mark_read):
            # Upstream returns ALL recent messages — the filter must happen
            # on our side using the pending queue.
            return {
                "messages": [
                    {"id": "msg-1", "content": "queued"},
                    {"id": "msg-2", "content": "already-read"},
                    {"id": "msg-3", "content": "even-older"},
                ],
                "unread_count": 0,
            }

    class _FactoryClient:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, attr):
            return getattr(FakeUpstreamClient(), attr)

    monkeypatch.setattr(_gw_agents, "AxClient", _FactoryClient)

    payload = _gw_messaging._inbox_for_managed_agent(name="cli_god", limit=10, unread_only=True)

    # Body must match header: only the messages in the pending queue.
    assert [m["id"] for m in payload["messages"]] == ["msg-1"]
    assert payload["unread_count"] == 1


class TestAgentsSendCommand:
    def test_json(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "message": {"id": "msg-1"},
            "content": "hello",
        }
        monkeypatch.setattr(_gw_messaging, "_send_from_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_messaging,
            "_send_from_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello"])
        assert result.exit_code != 0


class TestAgentsInboxCommand:
    def test_json(self, monkeypatch):
        payload = {"agent": "bot1", "messages": [], "unread_count": 0}
        monkeypatch.setattr(_gw_messaging, "_inbox_for_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "inbox", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_messaging,
            "_inbox_for_managed_agent",
            lambda **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "inbox", "nope"])
        assert result.exit_code != 0

    def test_value_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_messaging,
            "_inbox_for_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad param")),
        )
        result = runner.invoke(app, ["gateway", "agents", "inbox", "nope"])
        assert result.exit_code != 0


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
        monkeypatch.setattr(_gw_messaging, "_send_from_managed_agent", lambda **kw: payload)
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
        monkeypatch.setattr(_gw_messaging, "_send_from_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "send", "bot1", "hello"])
        assert result.exit_code == 0


class TestAgentsInboxTextRendering:
    def test_text_with_messages(self, monkeypatch):
        payload = {
            "agent": "bot1",
            "messages": [
                {"created_at": "2026-01-01", "display_name": "alice", "content": "hey"},
            ],
            "unread_count": 1,
        }
        monkeypatch.setattr(_gw_messaging, "_inbox_for_managed_agent", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "inbox", "bot1"])
        assert result.exit_code == 0


class TestAgentsInboxSpaceResolution:
    def test_inbox_space_cache_hit(self, monkeypatch):
        monkeypatch.setattr(_gw_messaging, "_resolve_space_via_cache", lambda v: "sp-1")
        monkeypatch.setattr(
            _gw_messaging,
            "_inbox_for_managed_agent",
            lambda **kw: {"agent": "bot1", "messages": []},
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "inbox", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_inbox_space_cache_miss(self, monkeypatch):
        monkeypatch.setattr(_gw_messaging, "_resolve_space_via_cache", lambda v: None)
        result = runner.invoke(
            app,
            ["gateway", "agents", "inbox", "bot1", "--space", "bad-slug"],
        )
        assert result.exit_code != 0
        assert "Could not resolve" in _strip(result.output)


def test_send_gateway_test_falls_back_to_user_author_in_offline_mode(monkeypatch, tmp_path):
    """AX_OFFLINE=1 with no invoking principal uses user-authored path instead of raising."""
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="echo-target", runtime_type="echo")
    entry["active_space_id"] = "space-1"
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "space-1"})

    sent_messages = []

    class _FakeClient:
        def send_message(self, space_id, content, **kwargs):
            sent_messages.append(content)
            return {"id": "m1", "content": content}

    with patch.object(_gw_messaging, "_load_gateway_user_client", return_value=_FakeClient()):
        with patch.object(_gw_messaging, "_resolve_invoking_principal", return_value=None):
            result = _gw_messaging._send_gateway_test_to_managed_agent("echo-target")

    assert result["author"] == "user"
    assert len(sent_messages) == 1
