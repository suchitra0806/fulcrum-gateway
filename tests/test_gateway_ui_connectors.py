"""Gateway UI /api/connectors route tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ax_cli.commands import gateway_ui as gw_cmd
from ax_cli.connectors.storage import add_connector
from ax_cli.connectors.types import ConnectorRow
from tests.gateway_cmd_testlib import _invoke_handler, _json_response


@pytest.fixture()
def tmp_connectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = tmp_path / "connectors.json"
    auth_dir = tmp_path / "connectors" / "auth"

    monkeypatch.setattr(
        "ax_cli.connectors.paths.connectors_registry_path",
        lambda: registry,
    )

    def _fake_auth_dir() -> Path:
        auth_dir.mkdir(parents=True, exist_ok=True)
        return auth_dir

    monkeypatch.setattr("ax_cli.connectors.paths.auth_dir", _fake_auth_dir)
    return tmp_path


@pytest.fixture()
def seeded_connector(tmp_connectors: Path) -> ConnectorRow:
    row = ConnectorRow.create(
        "demo-conn",
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


class TestConnectorApiRoutes:
    def test_get_connectors_list_empty(self, tmp_connectors: Path, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/api/connectors", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["count"] == 0
        assert data["connectors"] == []

    def test_get_connectors_providers(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/api/connectors/providers", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        names = {item["name"] for item in data["providers"]}
        assert "composio" in names
        assert "http_mcp" in names

    def test_post_create_connector(self, tmp_connectors: Path, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST",
            "/api/connectors",
            body={"name": "ui-conn", "provider": "composio", "managed_auth": True},
            monkeypatch=monkeypatch,
        )
        assert status == 201
        data = _json_response(status, body)
        assert data["connector"]["name"] == "ui-conn"
        assert data["connector"]["auth_ref"]

    def test_get_connector_detail(self, seeded_connector: ConnectorRow, monkeypatch):
        status, body, _ = _invoke_handler(
            "GET",
            f"/api/connectors/{seeded_connector.name}",
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["connector"]["name"] == "demo-conn"
        assert "auth_status" in data["connector"]

    def test_put_enable_disable_connector(self, seeded_connector: ConnectorRow, monkeypatch):
        status, body, _ = _invoke_handler(
            "PUT",
            f"/api/connectors/{seeded_connector.name}",
            body={"enabled": False},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["connector"]["enabled"] is False

        status, body, _ = _invoke_handler(
            "PUT",
            f"/api/connectors/{seeded_connector.name}",
            body={"enabled": True},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["connector"]["enabled"] is True

    def test_post_and_delete_auth(self, seeded_connector: ConnectorRow, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/auth",
            body={"COMPOSIO_API_KEY": "ak_test_key"},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert "COMPOSIO_API_KEY" in data["auth"]["keys"]
        assert "ak_test_key" not in json.dumps(data)

        status, body, _ = _invoke_handler(
            "GET",
            f"/api/connectors/{seeded_connector.name}/auth",
            monkeypatch=monkeypatch,
        )
        assert status == 200
        auth_data = _json_response(status, body)
        assert "COMPOSIO_API_KEY" in auth_data["auth"]["keys"]

        status, body, _ = _invoke_handler(
            "DELETE",
            f"/api/connectors/{seeded_connector.name}/auth",
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_delete_connector(self, seeded_connector: ConnectorRow, monkeypatch):
        status, body, _ = _invoke_handler(
            "DELETE",
            f"/api/connectors/{seeded_connector.name}",
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["removed"]["name"] == "demo-conn"

        status, body, _ = _invoke_handler(
            "GET",
            f"/api/connectors/{seeded_connector.name}",
            monkeypatch=monkeypatch,
        )
        assert status == 404

    def test_post_connect_and_apps_mocked(self, seeded_connector: ConnectorRow, monkeypatch):
        from ax_cli.connectors import gateway_api as connector_api

        _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/auth",
            body={"COMPOSIO_API_KEY": "ak_test_key"},
            monkeypatch=monkeypatch,
        )

        monkeypatch.setattr(
            connector_api,
            "initiate_connection",
            lambda row, app, entity_id, auth_env: {
                "connectionStatus": "INITIATED",
                "redirectUrl": "https://example.com/oauth",
            },
        )
        monkeypatch.setattr(
            connector_api,
            "list_apps",
            lambda row, auth_env: [{"appName": "gmail", "status": "ACTIVE", "clientUniqueUserId": "default"}],
        )

        status, body, _ = _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/connect",
            body={"app": "gmail"},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["redirect_url"] == "https://example.com/oauth"

        status, body, _ = _invoke_handler(
            "GET",
            f"/api/connectors/{seeded_connector.name}/apps",
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["count"] == 1
        assert data["apps"][0]["app"] == "gmail"

    def test_post_tools_search_and_call_mocked(self, seeded_connector: ConnectorRow, monkeypatch):
        from ax_cli.connectors import gateway_api as connector_api

        _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/auth",
            body={"COMPOSIO_API_KEY": "ak_test_key"},
            monkeypatch=monkeypatch,
        )

        monkeypatch.setattr(
            connector_api,
            "search_tools",
            lambda row, query, auth_env, apps=None, limit=10, mode="auto", session_id=None: {
                "items": [{"name": "GMAIL_SEND", "displayName": "Send Email"}],
                "mode": mode,
            },
        )
        monkeypatch.setattr(
            connector_api,
            "execute_tool",
            lambda row, tool, args, auth_env: {"ok": True, "tool": tool},
        )

        status, body, _ = _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/tools/search",
            body={"query": "send email", "limit": 5},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["count"] == 1

        status, body, _ = _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/tools/call",
            body={"tool": "GMAIL_SEND", "args": {"to": "a@example.com"}, "dry_run": True},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["dry_run"] is True
        assert data["tool"] == "GMAIL_SEND"

        status, body, _ = _invoke_handler(
            "POST",
            f"/api/connectors/{seeded_connector.name}/tools/call",
            body={"tool": "GMAIL_SEND", "args": {"to": "a@example.com"}},
            monkeypatch=monkeypatch,
        )
        assert status == 200
        data = _json_response(status, body)
        assert data["result"]["ok"] is True

    def test_status_includes_connector_counts(self, seeded_connector: ConnectorRow, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/api/status", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["connectors_count"] == 1
        assert data["enabled_connectors"] == 1
        assert "connectors_registry_path" in data


class TestConnectorUiPage:
    def test_render_gateway_ui_page_includes_connectors_panel(self):
        page = gw_cmd._render_gateway_ui_page(refresh_ms=2000)
        assert "Outbound Connectors" in page
        assert "/api/connectors" in page
        assert "connector-rows" in page
        assert "loadConnectors" in page
