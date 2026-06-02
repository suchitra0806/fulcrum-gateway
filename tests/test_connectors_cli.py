"""CLI integration tests for connector commands via CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ax_cli.commands.gateway import connectors_app
from ax_cli.connectors.storage import add_connector
from ax_cli.connectors.types import ConnectorRow

runner = CliRunner()


@pytest.fixture()
def tmp_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "ax_cli.connectors.paths.connectors_registry_path",
        lambda: tmp_path / "connectors.json",
    )
    auth_dir = tmp_path / "connectors" / "auth"

    def _fake_auth_dir() -> Path:
        auth_dir.mkdir(parents=True, exist_ok=True)
        return auth_dir

    monkeypatch.setattr("ax_cli.connectors.paths.auth_dir", _fake_auth_dir)
    return tmp_path


@pytest.fixture()
def seeded_connector(tmp_gateway: Path) -> ConnectorRow:
    row = ConnectorRow.create(
        "test-conn",
        "composio",
        managed_auth=True,
        config={
            "composio_base_url": "https://backend.composio.dev/api/v3",
            "entity_id": "default",
            "connected_account_id": None,
            "app_name": None,
            "classification": None,
        },
    )
    add_connector(row)
    return row


# ── connectors list ───────────────────────────────────────────────────────────


class TestConnectorsList:
    def test_list_empty(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["list"])
        assert result.exit_code == 0
        assert "No connectors" in result.output

    def test_list_with_connector(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["list"])
        assert result.exit_code == 0
        assert "test-conn" in result.output

    def test_list_json(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "test-conn"


# ── connectors show ──────────────────────────────────────────────────────────


class TestConnectorsShow:
    def test_show_existing(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["show", "test-conn"])
        assert result.exit_code == 0
        assert "test-conn" in result.output
        assert "composio" in result.output

    def test_show_not_found(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["show", "nonexistent"])
        assert result.exit_code == 1

    def test_show_json(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["show", "test-conn", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "test-conn"
        assert data["provider"] == "composio"


# ── connectors add ───────────────────────────────────────────────────────────


class TestConnectorsAdd:
    def test_add_composio(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["add", "new-conn", "--provider", "composio", "--managed-auth"])
        assert result.exit_code == 0
        assert "Added connector" in result.output
        assert "new-conn" in result.output

    def test_add_unknown_provider(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["add", "bad", "--provider", "unknown_xyz"])
        assert result.exit_code == 1
        assert "Unknown provider" in result.output

    def test_add_duplicate_name(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["add", "test-conn", "--provider", "composio"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_add_json(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["add", "json-conn", "--provider", "composio", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "json-conn"
        assert data["provider"] == "composio"


# ── connectors remove ────────────────────────────────────────────────────────


class TestConnectorsRemove:
    def test_remove_existing(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["remove", "test-conn"])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_not_found(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["remove", "nonexistent"])
        assert result.exit_code == 1


# ── connectors set ───────────────────────────────────────────────────────────


class TestConnectorsSet:
    def test_set_config_key(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["set", "test-conn", "entity_id", "user123"])
        assert result.exit_code == 0
        assert "entity_id" in result.output
        assert "user123" in result.output

    def test_set_not_found(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["set", "nonexistent", "key", "val"])
        assert result.exit_code == 1

    def test_set_policy_json_array(self, seeded_connector: ConnectorRow):
        result = runner.invoke(
            connectors_app,
            ["set", "test-conn", "allowed_tools", '["GITHUB_*", "JIRA_*"]', "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config"]["allowed_tools"] == ["GITHUB_*", "JIRA_*"]

    def test_set_policy_comma_separated(self, seeded_connector: ConnectorRow):
        result = runner.invoke(
            connectors_app,
            ["set", "test-conn", "denied_toolkits", "slack,salesforce", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config"]["denied_toolkits"] == ["slack", "salesforce"]

    def test_set_policy_single_value(self, seeded_connector: ConnectorRow):
        result = runner.invoke(
            connectors_app,
            ["set", "test-conn", "allowed_toolkits", "github", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config"]["allowed_toolkits"] == ["github"]

    def test_set_non_policy_key_stays_string(self, seeded_connector: ConnectorRow):
        result = runner.invoke(
            connectors_app,
            ["set", "test-conn", "entity_id", '["not", "parsed"]', "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config"]["entity_id"] == '["not", "parsed"]'


# ── connectors providers ─────────────────────────────────────────────────────


class TestConnectorsProviders:
    def test_providers_list(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["providers"])
        assert result.exit_code == 0
        assert "composio" in result.output.lower()
        assert "COMPOSIO_API_KEY" in result.output

    def test_providers_json(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["providers", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) >= 1
        assert data[0]["name"] == "composio"


# ── connectors auth write ────────────────────────────────────────────────────


class TestConnectorsAuthWrite:
    def test_auth_write(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["auth", "write", "test-conn", "COMPOSIO_API_KEY=ak_test"])
        assert result.exit_code == 0
        assert "Auth written" in result.output
        assert "COMPOSIO_API_KEY" in result.output

    def test_auth_write_invalid_format(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["auth", "write", "test-conn", "NOEQUALS"])
        assert result.exit_code == 1
        assert "KEY=VALUE" in result.output

    def test_auth_write_not_found(self, tmp_gateway: Path):
        result = runner.invoke(connectors_app, ["auth", "write", "nonexistent", "KEY=val"])
        assert result.exit_code == 1


# ── connectors auth status ───────────────────────────────────────────────────


class TestConnectorsAuthStatus:
    def test_auth_status_missing(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["auth", "status", "test-conn"])
        assert result.exit_code == 0
        assert "No auth configured" in result.output or "not configured" in result.output.lower()

    def test_auth_status_existing(self, seeded_connector: ConnectorRow):
        runner.invoke(connectors_app, ["auth", "write", "test-conn", "COMPOSIO_API_KEY=ak_test"])
        result = runner.invoke(connectors_app, ["auth", "status", "test-conn"])
        assert result.exit_code == 0
        assert "COMPOSIO_API_KEY" in result.output

    def test_auth_status_json(self, seeded_connector: ConnectorRow):
        runner.invoke(connectors_app, ["auth", "write", "test-conn", "KEY=val"])
        result = runner.invoke(connectors_app, ["auth", "status", "test-conn", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["exists"] is True
        assert "KEY" in data["keys"]


# ── connectors auth clear ────────────────────────────────────────────────────


class TestConnectorsAuthClear:
    def test_auth_clear_existing(self, seeded_connector: ConnectorRow):
        runner.invoke(connectors_app, ["auth", "write", "test-conn", "KEY=val"])
        result = runner.invoke(connectors_app, ["auth", "clear", "test-conn"])
        assert result.exit_code == 0
        assert "Auth removed" in result.output

    def test_auth_clear_missing(self, seeded_connector: ConnectorRow):
        result = runner.invoke(connectors_app, ["auth", "clear", "test-conn"])
        assert result.exit_code == 0
        assert "No auth file" in result.output
