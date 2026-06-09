"""Per-module agents command tests (gateway split #28 Phase 1 follow-up).

Rewritten counterparts of the ``agents`` command tests in the skipped
``test_gateway_commands.py``. Monkeypatches target the owning module
(``ax_cli.commands.gateway_agents``) where ``agents add`` resolves the bootstrap
and auth helpers it calls (whether top-imported from ``bootstrap`` or
bottom-imported from sibling modules). See
docs/refactor/split-commands-gateway-removal.md.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as gw
from ax_cli.main import app

runner = CliRunner()


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


def test_gateway_agents_add_template_help_lists_full_catalog():
    result = runner.invoke(app, ["gateway", "agents", "add", "--help"])
    assert result.exit_code == 0, result.output


def test_gateway_agents_add_mints_token_and_writes_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gw, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gw, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gw,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-1", "name": "echo-bot"},
    )
    monkeypatch.setattr(gw, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gw, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(app, ["gateway", "agents", "add", "echo-bot", "--type", "echo", "--timeout", "42", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "echo-bot"
    assert payload["runtime_type"] == "echo"
    assert payload["timeout_seconds"] == 42
    assert payload["desired_state"] == "running"
    assert payload["credential_source"] == "gateway"
    assert payload["transport"] == "gateway"
    registry = gateway_core.load_gateway_registry()
    assert registry["agents"][0]["name"] == "echo-bot"
    assert registry["agents"][0]["timeout_seconds"] == 42
    assert registry["bindings"][0]["asset_id"] == "agent-1"
    assert registry["bindings"][0]["approved_state"] == "approved"
    assert registry["agents"][0]["install_id"] == registry["bindings"][0]["install_id"]
    assert registry["agents"][0]["token_file"] == "agents/echo-bot/token"
    token_file = gateway_core.resolve_agent_token_file(registry["agents"][0])
    assert token_file.is_absolute()
    assert token_file.exists()
    assert token_file.read_text().strip() == "axp_a_agent.secret"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "managed_agent_added"
    assert recent[-1]["agent_name"] == "echo-bot"
