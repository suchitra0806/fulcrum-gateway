"""Per-module gateway command tests: gateway_spaces (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_spaces."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_spaces as _gw_spaces
from ax_cli.main import app
from tests.gateway_cmd_testlib import _GOOD_SPACE_UUID, _strip, _wire_gateway_spaces_use

runner = CliRunner()


def test_existing_agent_home_space_prefers_backend_default_space():
    class FakeClient:
        def list_agents(self):
            return [
                {"name": "other", "space_id": "space-other"},
                {"name": "backend_sentinel", "default_space_id": "space-from-db", "space_id": "space-row"},
            ]

    assert _gw_spaces._existing_agent_home_space(FakeClient(), "backend_sentinel") == "space-row"


def test_existing_agent_home_space_prefers_backend_current_space():
    assert (
        _gw_spaces._agent_space_id_from_backend_record(
            {
                "name": "backend_sentinel",
                "current_space": {"id": "space-current", "name": "Current"},
                "space_id": "space-row",
                "default_space_id": "space-default",
            }
        )
        == "space-current"
    )


def test_gateway_spaces_use_resolves_slug_and_updates_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    private_uuid = "11111111-2222-3333-4444-555555555555"
    team_uuid = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": private_uuid,
            "space_name": "madtank-workspace",
            "username": "codex",
        }
    )

    class FakeClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": private_uuid, "slug": "madtank-workspace", "name": "madtank's Workspace"},
                    {"id": team_uuid, "slug": "ax-cli-dev", "name": "aX CLI Dev"},
                ]
            }

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: FakeClient())

    result = runner.invoke(app, ["gateway", "spaces", "use", "ax-cli-dev", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["space_id"] == team_uuid
    assert payload["space_name"] == "aX CLI Dev"
    session = gateway_core.load_gateway_session()
    assert session["space_id"] == team_uuid
    assert session["space_name"] == "aX CLI Dev"
    # Active space lives only in session.json — registry.gateway must NOT
    # carry a duplicate copy (post-simplification: single source of truth).
    registry = gateway_core.load_gateway_registry()
    assert "space_id" not in registry["gateway"]
    assert "space_name" not in registry["gateway"]
    # Resolved id/name should be persisted to the spaces cache so a subsequent
    # slug switch can short-circuit list_spaces.
    cached = gateway_core.load_space_cache()
    cached_ids = {row.get("id") for row in cached}
    assert team_uuid in cached_ids


def test_gateway_spaces_current_shows_session_space(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "team-space",
            "space_name": "ax-cli-dev",
            "username": "codex",
        }
    )

    result = runner.invoke(app, ["gateway", "spaces", "current", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "space_id": "team-space",
        "space_name": "ax-cli-dev",
        "base_url": "https://paxai.app",
        "username": "codex",
    }


def test_resolve_gateway_agent_home_space_resolves_name_to_uuid(monkeypatch):
    captured = {}

    def fake_resolve(client, *, explicit):
        captured["explicit"] = explicit
        return _GOOD_SPACE_UUID

    monkeypatch.setattr(_gw_spaces, "resolve_space_id", fake_resolve)

    resolved = _gw_spaces._resolve_gateway_agent_home_space(
        client=object(),
        session={},
        registry={"agents": []},
        explicit_space_id="madtank's Workspace",
    )
    assert resolved == _GOOD_SPACE_UUID
    assert captured["explicit"] == "madtank's Workspace"


def test_resolve_gateway_agent_home_space_passthrough_for_uuid(monkeypatch):
    def fake_resolve(*args, **kwargs):
        raise AssertionError("UUID input should not require a backend round-trip")

    monkeypatch.setattr(_gw_spaces, "resolve_space_id", fake_resolve)

    resolved = _gw_spaces._resolve_gateway_agent_home_space(
        client=object(),
        session={},
        registry={"agents": []},
        explicit_space_id=_GOOD_SPACE_UUID,
    )
    assert resolved == _GOOD_SPACE_UUID


def test_spaces_payload_returns_session_active_space_when_upstream_fails(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": _GOOD_SPACE_UUID,
            "space_name": "madtank's Workspace",
            "username": "madtank",
        }
    )

    def fake_client_loader():
        class Boom:
            def list_spaces(self):
                raise httpx.HTTPStatusError(
                    "429 Too Many Requests",
                    request=httpx.Request("GET", "https://paxai.app/api/v1/spaces"),
                    response=httpx.Response(429),
                )

        return Boom()

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", fake_client_loader)

    payload = _gw_spaces._spaces_payload()
    assert payload["active_space_id"] == _GOOD_SPACE_UUID
    assert payload["active_space_name"] == "madtank's Workspace"
    # Active space surfaces in the spaces list even with no cache so the UI
    # always has something to render.
    assert any(s["id"] == _GOOD_SPACE_UUID for s in payload["spaces"])
    assert "error" in payload


def test_spaces_payload_uses_cached_spaces_after_upstream_failure(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": _GOOD_SPACE_UUID,
            "space_name": "madtank's Workspace",
        }
    )

    other_space = "78950af5-4d27-441b-9296-ec46de8ba35d"

    class FirstClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": _GOOD_SPACE_UUID, "name": "madtank's Workspace"},
                    {"id": other_space, "name": "Other Workspace"},
                ]
            }

    class FailingClient:
        def list_spaces(self):
            raise RuntimeError("upstream rate limited")

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: FirstClient())
    first = _gw_spaces._spaces_payload()
    assert {s["id"] for s in first["spaces"]} == {_GOOD_SPACE_UUID, other_space}

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: FailingClient())
    second = _gw_spaces._spaces_payload()
    assert second.get("cached") is True
    assert {s["id"] for s in second["spaces"]} == {_GOOD_SPACE_UUID, other_space}


def test_gateway_spaces_list_command_renders_table(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": _GOOD_SPACE_UUID,
            "space_name": "madtank's Workspace",
        }
    )

    class StubClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": _GOOD_SPACE_UUID, "name": "madtank's Workspace", "slug": "madtank"},
                ]
            }

    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: StubClient())

    result = runner.invoke(app, ["gateway", "spaces", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active_space_id"] == _GOOD_SPACE_UUID
    assert payload["spaces"][0]["id"] == _GOOD_SPACE_UUID


def test_normalize_spaces_response_hydrates_name_from_cache(monkeypatch, tmp_path):
    """If upstream returns a row with a missing/empty name, the UI must
    surface the cached friendly name for previously-seen spaces instead of
    falling back to the raw UUID."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache(
        [
            {"id": _GOOD_SPACE_UUID, "name": "madtank's Workspace", "slug": "madtank"},
        ]
    )

    upstream_partial = [
        {"id": _GOOD_SPACE_UUID, "name": "", "slug": "madtank"},
    ]
    rows = _gw_spaces._normalize_spaces_response(upstream_partial)
    assert rows[0]["name"] == "madtank's Workspace"


def test_resolve_space_via_cache_passes_uuid_through_unchanged():
    uuid_in = "12345678-1234-4234-8234-123456789012"
    assert _gw_spaces._resolve_space_via_cache(uuid_in) == uuid_in


def test_resolve_space_via_cache_resolves_slug_via_cache(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache(
        [
            {"id": "12345678-1234-4234-8234-123456789012", "name": "ax-cli-dev", "slug": "ax-cli-dev"},
            {"id": "abcdef01-2345-4234-8234-123456789012", "name": "Other", "slug": "other"},
        ]
    )

    assert _gw_spaces._resolve_space_via_cache("ax-cli-dev") == "12345678-1234-4234-8234-123456789012"


def test_resolve_space_via_cache_returns_none_for_unknown_slug(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_space_cache([])

    assert _gw_spaces._resolve_space_via_cache("never-seen") is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_resolve_space_via_cache_returns_none_for_empty_input(value):
    assert _gw_spaces._resolve_space_via_cache(value) is None


def test_gateway_spaces_use_syncs_both_stores(monkeypatch, tmp_path):
    captured = _wire_gateway_spaces_use(monkeypatch, tmp_path)

    result = runner.invoke(app, ["gateway", "spaces", "use", "space-new", "--json"])

    assert result.exit_code == 0, result.output
    # Gateway session updated...
    assert gateway_core.load_gateway_session()["space_id"] == "space-new"
    # ...and the CLI config store synced too (default local).
    assert captured == {"sid": "space-new", "local": True}
    payload = json.loads(result.stdout)
    assert payload["space_id"] == "space-new"
    assert payload["cli_scope"] == "local"
    assert payload["gateway_session"]["updated"] is True


def test_gateway_spaces_use_global_writes_global_cli_config(monkeypatch, tmp_path):
    captured = _wire_gateway_spaces_use(monkeypatch, tmp_path)

    result = runner.invoke(app, ["gateway", "spaces", "use", "space-new", "--global", "--json"])

    assert result.exit_code == 0, result.output
    assert captured == {"sid": "space-new", "local": False}
    assert json.loads(result.stdout)["cli_scope"] == "global"


class TestSpacesCurrentCommand:
    def test_json(self, monkeypatch):
        monkeypatch.setattr(
            _gw_spaces,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "space_name": "Test", "base_url": "https://paxai.app", "username": "u"},
        )
        result = runner.invoke(app, ["gateway", "spaces", "current", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["space_id"] == "sp-1"

    def test_text(self, monkeypatch):
        monkeypatch.setattr(
            _gw_spaces,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "space_name": "Test", "base_url": "https://paxai.app", "username": "u"},
        )
        result = runner.invoke(app, ["gateway", "spaces", "current"])
        assert result.exit_code == 0


class TestSpacesListCommand:
    def test_json(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
        }
        monkeypatch.setattr(_gw_spaces, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["spaces"]) == 1

    def test_text_with_spaces(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
        }
        monkeypatch.setattr(_gw_spaces, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0

    def test_text_no_spaces(self, monkeypatch):
        monkeypatch.setattr(_gw_spaces, "_spaces_payload", lambda: {"spaces": [], "active_space_id": None})
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0
        assert "No spaces" in _strip(result.output)


class TestSpacesUseCommand:
    def test_json(self, monkeypatch):
        # As of #82 the command is a full alias: it writes the gateway session
        # (via apply_space_to_gateway_session) AND the CLI config (save_space_id).
        monkeypatch.setattr(
            _gw_spaces,
            "_load_gateway_session_or_exit",
            lambda: {"space_id": "sp-1", "token": "axp_u_x", "base_url": "https://paxai.app"},
        )
        monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: MagicMock())
        monkeypatch.setattr(_gw_spaces, "resolve_space_id", lambda client, explicit: "sp-2")
        monkeypatch.setattr(_gw_spaces, "_space_name_for_id", lambda client, sid: "New Space")
        monkeypatch.setattr(
            _gw_spaces,
            "apply_space_to_gateway_session",
            lambda sid, *, space_name=None: {
                "updated": True,
                "session_path": "/tmp/session.json",
                "previous_space_id": "sp-1",
                "space_id": sid,
                "space_name": space_name,
                "daemon_running": False,
            },
        )
        saved = {}
        monkeypatch.setattr("ax_cli.config.save_space_id", lambda sid, **kw: saved.update(sid=sid, **kw))
        result = runner.invoke(app, ["gateway", "spaces", "use", "sp-2", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["space_id"] == "sp-2"
        assert data["cli_scope"] == "local"
        assert data["gateway_session"]["updated"] is True
        assert saved == {"sid": "sp-2", "local": True}


class TestSpacesListErrors:
    def test_list_with_upstream_error(self, monkeypatch):
        payload = {
            "spaces": [{"id": "sp-1", "name": "Work", "slug": "work"}],
            "active_space_id": "sp-1",
            "error": "upstream 503",
            "cached": True,
        }
        monkeypatch.setattr(_gw_spaces, "_spaces_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "spaces", "list"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "cached" in output.lower() or "upstream" in output.lower()
