"""Offline mode: AX_OFFLINE=1 behaviour for get_client, gateway session,
hermes env, channel setup, smoke command, and OfflineAgentQueues.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway as gateway_cmd
from ax_cli.offline_client import OfflineAxClient
from ax_cli.offline_sse import OfflineAgentQueues, agent_name_from_token, extract_mentions, make_token

# ---------------------------------------------------------------------------
# OfflineAgentQueues
# ---------------------------------------------------------------------------


def test_subscribe_and_deliver():
    bus = OfflineAgentQueues()
    q = bus.subscribe("alpha")
    assert bus.is_subscribed("alpha")
    assert bus.deliver("alpha", {"id": "1", "content": "hi"})
    msg = q.get_nowait()
    assert msg["id"] == "1"


def test_deliver_returns_false_when_not_subscribed():
    bus = OfflineAgentQueues()
    assert not bus.deliver("nobody", {"id": "x"})


def test_unsubscribe_removes_queue():
    bus = OfflineAgentQueues()
    bus.subscribe("beta")
    bus.unsubscribe("beta")
    assert not bus.is_subscribed("beta")


def test_subscribe_replaces_previous_queue():
    bus = OfflineAgentQueues()
    q1 = bus.subscribe("gamma")
    q2 = bus.subscribe("gamma")
    assert q1 is not q2
    bus.deliver("gamma", {"id": "2"})
    assert q2.qsize() == 1
    assert q1.qsize() == 0


def test_deliver_is_case_insensitive():
    bus = OfflineAgentQueues()
    q = bus.subscribe("MyAgent")
    assert bus.deliver("myagent", {"id": "3"})
    assert q.qsize() == 1


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def test_make_and_decode_token():
    assert make_token("claude-test") == "offline-claude-test"
    assert agent_name_from_token("offline-claude-test") == "claude-test"


def test_agent_name_from_token_rejects_non_offline():
    assert agent_name_from_token("axp_a_something") is None
    assert agent_name_from_token("") is None


def test_extract_mentions():
    assert extract_mentions("@echo-bot say hello") == ["echo-bot"]
    assert extract_mentions("no mentions here") == []
    assert extract_mentions("@alice and @bob") == ["alice", "bob"]


# ---------------------------------------------------------------------------
# OfflineAxClient
# ---------------------------------------------------------------------------


def test_offline_client_default_base_url(monkeypatch):
    monkeypatch.delenv("AX_LOCAL_GATEWAY_URL", raising=False)
    client = OfflineAxClient()
    assert client.base_url == "http://localhost:8765"


def test_offline_client_respects_gateway_url_env(monkeypatch):
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    client = OfflineAxClient()
    assert client.base_url == "http://localhost:9999"


def test_offline_client_explicit_base_url_wins(monkeypatch):
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    client = OfflineAxClient(base_url="http://custom:1234")
    assert client.base_url == "http://custom:1234"


def test_get_client_returns_offline_client_when_flag_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    from ax_cli.config import get_client
    client = get_client()
    assert isinstance(client, OfflineAxClient)
    assert client.base_url == "http://localhost:8765"


def test_get_client_not_offline_without_flag(monkeypatch, tmp_path):
    import typer
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    from ax_cli.config import get_client
    # Without a token, should raise typer.Exit(1) — not return OfflineAxClient
    with pytest.raises(typer.Exit):
        get_client()


# ---------------------------------------------------------------------------
# Gateway session in offline mode
# ---------------------------------------------------------------------------


def test_load_gateway_session_or_exit_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    session = gateway_cmd._load_gateway_session_or_exit()
    assert session["base_url"] == "http://localhost:8765"
    assert session["token"] == "offline"


def test_load_gateway_session_or_exit_offline_custom_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    session = gateway_cmd._load_gateway_session_or_exit()
    assert session["base_url"] == "http://localhost:9999"


def test_load_gateway_user_client_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    client = gateway_cmd._load_gateway_user_client()
    assert isinstance(client, OfflineAxClient)


# ---------------------------------------------------------------------------
# Hermes plugin env in offline mode
# ---------------------------------------------------------------------------


def test_hermes_plugin_env_uses_gateway_url_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_test.token")
    entry = {
        "name": "my-hermes",
        "agent_id": "agent-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
    }
    with patch.object(gateway_core, "load_gateway_managed_agent_token", return_value="axp_a_test.token"):
        with patch.object(gateway_core, "_hermes_plugin_home", return_value=tmp_path / "home"):
            env = gateway_core._build_hermes_plugin_env(entry)
    assert env["AX_BASE_URL"] == "http://localhost:8765"
    assert env["AX_OFFLINE"] == "1"


def test_hermes_plugin_env_uses_paxai_when_not_offline(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    entry = {"name": "my-hermes", "agent_id": "agent-1", "space_id": "space-1", "base_url": "https://paxai.app"}
    with patch.object(gateway_core, "load_gateway_managed_agent_token", return_value="axp_a_tok"):
        with patch.object(gateway_core, "_hermes_plugin_home", return_value=tmp_path / "home"):
            env = gateway_core._build_hermes_plugin_env(entry)
    assert env["AX_BASE_URL"] == "https://paxai.app"
    assert "AX_OFFLINE" not in env


# ---------------------------------------------------------------------------
# channel setup writes AX_OFFLINE into env file
# ---------------------------------------------------------------------------


def _seed_offline_gateway(tmp_path, monkeypatch):
    """Write a minimal offline gateway session and agent token for channel setup."""
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session({
        "token": "offline",
        "base_url": "http://localhost:8765",
        "space_id": "00000000-0000-0000-0000-000000000001",
    })
    agent_dir = tmp_path / "ax_config" / "gateway" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "token").write_text("axp_a_offline_abc123")
    registry = {
        "agents": [{
            "name": "my-agent",
            "agent_id": "agent-offline-1",
            "space_id": "00000000-0000-0000-0000-000000000001",
            "base_url": "http://localhost:8765",
            "token_file": str(agent_dir / "token"),
        }]
    }
    gateway_core.save_gateway_registry(registry)


def test_channel_setup_writes_ax_offline_to_env_file(monkeypatch, tmp_path):
    _seed_offline_gateway(tmp_path, monkeypatch)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    env_path = tmp_path / "claude-channel.env"
    from ax_cli.commands.channel import write_channel_setup
    write_channel_setup(
        agent_name="my-agent",
        workdir=workdir,
        env_path=env_path,
    )
    env_text = env_path.read_text()
    assert 'AX_OFFLINE="1"' in env_text
    assert 'AX_BASE_URL="http://localhost:8765"' in env_text


def test_channel_setup_no_ax_offline_without_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session({
        "token": "axp_u_real.token",
        "base_url": "https://paxai.app",
        "space_id": "space-1",
    })
    agent_dir = tmp_path / "ax_config" / "gateway" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "token").write_text("axp_a_real.token")
    gateway_core.save_gateway_registry({"agents": [{
        "name": "my-agent", "agent_id": "aid", "space_id": "space-1",
        "base_url": "https://paxai.app", "token_file": str(agent_dir / "token"),
    }]})
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    env_path = tmp_path / "claude-channel.env"
    from ax_cli.commands.channel import write_channel_setup
    write_channel_setup(agent_name="my-agent", workdir=workdir, env_path=env_path)
    env_text = env_path.read_text()
    assert "AX_OFFLINE" not in env_text


# ---------------------------------------------------------------------------
# smoke command — echo runtime (in-process, no gateway needed)
# ---------------------------------------------------------------------------


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


def test_smoke_echo_returns_echo_response(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="echo-bot", runtime_type="echo")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    from typer.testing import CliRunner

    from ax_cli.main import app
    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "agents", "smoke", "echo-bot", "--message", "hello"])
    assert result.exit_code == 0
    assert "Echo: hello" in result.output


def test_smoke_echo_uses_recommended_test_message(monkeypatch, tmp_path):
    """Without --message, smoke uses the template's recommended_test_message."""
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="echo-bot", runtime_type="echo")
    entry["template_id"] = "echo_test"  # recommended_test_message = "gateway test ping"
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    from typer.testing import CliRunner

    from ax_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "agents", "smoke", "echo-bot"])
    assert result.exit_code == 0
    assert "Echo: gateway test ping" in result.output


# ---------------------------------------------------------------------------
# smoke command — channel runtime (HTTP delivery path)
# ---------------------------------------------------------------------------


def test_smoke_channel_not_connected_when_no_subscriber(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="my-channel", runtime_type="claude_code_channel")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    # Gateway returns empty delivered_to — agent not subscribed
    import httpx
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 201
    fake_resp.json.return_value = {"id": "msg-1", "delivered_to": [], "message": {}}
    fake_resp.raise_for_status = MagicMock()

    from typer.testing import CliRunner

    from ax_cli.main import app
    runner = CliRunner()
    with patch("httpx.post", return_value=fake_resp):
        result = runner.invoke(app, ["gateway", "agents", "smoke", "my-channel"])
    assert result.exit_code == 1
    assert "not connected" in result.output


def test_smoke_channel_shows_reply_from_log(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="my-channel", runtime_type="claude_code_channel")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    import httpx
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 201
    fake_resp.json.return_value = {"id": "sent-msg-1", "delivered_to": ["my-channel"], "message": {"id": "sent-msg-1"}}
    fake_resp.raise_for_status = MagicMock()

    replies_path = tmp_path / "ax_config" / "gateway" / "offline-replies.jsonl"
    replies_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the reply in a background thread after a short delay so it lands
    # AFTER the smoke command records start_pos (which happens post-send).
    def _write_reply():
        import time
        time.sleep(0.2)
        with replies_path.open("a") as f:
            f.write(json.dumps({"id": "reply-1", "content": "pong", "author": "my-channel"}) + "\n")

    t = threading.Thread(target=_write_reply, daemon=True)

    from typer.testing import CliRunner

    from ax_cli.main import app
    runner = CliRunner()

    def start_reply_thread(*args, **kwargs):
        t.start()
        return fake_resp

    with patch("httpx.post", side_effect=start_reply_thread):
        with patch.object(gateway_cmd, "_offline_replies_path", return_value=replies_path):
            result = runner.invoke(app, ["gateway", "agents", "smoke", "my-channel", "--message", "ping"])
    assert result.exit_code == 0
    assert "pong" in result.output


# ---------------------------------------------------------------------------
# invoking principal fallback in offline mode
# ---------------------------------------------------------------------------


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

    with patch.object(gateway_cmd, "_load_gateway_user_client", return_value=_FakeClient()):
        with patch.object(gateway_cmd, "_resolve_invoking_principal", return_value=None):
            result = gateway_cmd._send_gateway_test_to_managed_agent("echo-target")

    assert result["author"] == "user"
    assert len(sent_messages) == 1
