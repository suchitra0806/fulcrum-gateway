"""Tests for the Composio HTTP adapter with mocked responses."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from ax_cli.connectors.errors import ConnectorAuthError, ConnectorProviderError
from ax_cli.connectors.providers.composio_adapter import (
    DEFAULT_BASE_URL,
    _api_key,
    _base_url,
    execute_tool,
    search_tools,
)


@pytest.fixture()
def auth_env() -> dict[str, str]:
    return {"COMPOSIO_API_KEY": "ak_test_key"}


@pytest.fixture()
def config() -> dict[str, Any]:
    return {
        "composio_base_url": DEFAULT_BASE_URL,
        "entity_id": "default",
        "connected_account_id": None,
        "app_name": None,
    }


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    if json_data is not None:
        return httpx.Response(
            status_code=status_code,
            json=json_data,
            request=httpx.Request("GET", "https://example.com"),
        )
    return httpx.Response(
        status_code=status_code,
        text="",
        request=httpx.Request("GET", "https://example.com"),
    )


# ── Config helpers ────────────────────────────────────────────────────────────


class TestConfigHelpers:
    def test_base_url_default(self):
        assert _base_url({}) == DEFAULT_BASE_URL

    def test_base_url_custom(self):
        assert _base_url({"composio_base_url": "https://custom.dev/api/v2/"}) == "https://custom.dev/api/v2"

    def test_api_key_present(self):
        assert _api_key({"COMPOSIO_API_KEY": "ak_test"}, "conn") == "ak_test"

    def test_api_key_missing(self):
        with pytest.raises(ConnectorAuthError, match="COMPOSIO_API_KEY"):
            _api_key({}, "conn")

    def test_api_key_empty(self):
        with pytest.raises(ConnectorAuthError, match="COMPOSIO_API_KEY"):
            _api_key({"COMPOSIO_API_KEY": "  "}, "conn")


# ── Search tools ──────────────────────────────────────────────────────────────


class TestSearchTools:
    def test_search_success(self, auth_env: dict, config: dict):
        mock_data = {
            "items": [
                {"name": "GITHUB_LIST_PRS", "displayName": "List PRs", "description": "Lists PRs"},
            ],
            "page": 1,
            "totalPages": 1,
        }
        with patch("httpx.get", return_value=_mock_response(200, mock_data)) as mock_get:
            result = search_tools("list github PRs", auth_env, config, "test-conn", limit=5)
            assert result["items"][0]["name"] == "GITHUB_LIST_PRS"
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args
            assert call_kwargs.kwargs["params"]["useCase"] == "list github PRs"
            assert call_kwargs.kwargs["params"]["limit"] == 5
            assert call_kwargs.kwargs["headers"]["x-api-key"] == "ak_test_key"

    def test_search_auth_error(self, auth_env: dict, config: dict):
        with patch("httpx.get", return_value=_mock_response(401, {"error": "Invalid API key"})):
            with pytest.raises(ConnectorProviderError) as exc_info:
                search_tools("test", auth_env, config, "test-conn")
            assert exc_info.value.status_code == 401

    def test_search_rate_limited(self, auth_env: dict, config: dict):
        with patch("httpx.get", return_value=_mock_response(429, {"error": "Rate limited"})):
            with pytest.raises(ConnectorProviderError) as exc_info:
                search_tools("test", auth_env, config, "test-conn")
            assert exc_info.value.status_code == 429

    def test_search_timeout(self, auth_env: dict, config: dict):
        with patch("httpx.get", side_effect=httpx.ReadTimeout("read timeout")):
            with pytest.raises(ConnectorProviderError, match="Timeout"):
                search_tools("test", auth_env, config, "test-conn")

    def test_search_missing_auth(self, config: dict):
        with pytest.raises(ConnectorAuthError, match="COMPOSIO_API_KEY"):
            search_tools("test", {}, config, "test-conn")


# ── Execute tool ──────────────────────────────────────────────────────────────


class TestExecuteTool:
    def test_execute_with_entity_id(self, auth_env: dict, config: dict):
        mock_data = {"successful": True, "data": {"result": "ok"}}
        with patch("httpx.post", return_value=_mock_response(200, mock_data)) as mock_post:
            result = execute_tool("GITHUB_LIST_PRS", {"owner": "test"}, auth_env, config, "test-conn")
            assert result["successful"] is True
            call_kwargs = mock_post.call_args
            body = call_kwargs.kwargs["json"]
            assert body["input"] == {"owner": "test"}
            assert body["entityId"] == "default"
            assert "connectedAccountId" not in body

    def test_execute_with_connected_account(self, auth_env: dict, config: dict):
        config["connected_account_id"] = "acct_123"
        mock_data = {"successful": True, "data": {}}
        with patch("httpx.post", return_value=_mock_response(200, mock_data)) as mock_post:
            execute_tool("SLACK_SEND_MSG", {}, auth_env, config, "test-conn")
            body = mock_post.call_args.kwargs["json"]
            assert body["connectedAccountId"] == "acct_123"
            assert "entityId" not in body

    def test_execute_with_auth_env_account_id(self, config: dict):
        auth = {"COMPOSIO_API_KEY": "ak_test", "COMPOSIO_CONNECTED_ACCOUNT_ID": "acct_env"}
        mock_data = {"successful": True, "data": {}}
        with patch("httpx.post", return_value=_mock_response(200, mock_data)) as mock_post:
            execute_tool("TEST_ACTION", {}, auth, config, "test-conn")
            body = mock_post.call_args.kwargs["json"]
            assert body["connectedAccountId"] == "acct_env"

    def test_execute_with_app_name(self, auth_env: dict, config: dict):
        config["app_name"] = "github"
        mock_data = {"successful": True, "data": {}}
        with patch("httpx.post", return_value=_mock_response(200, mock_data)) as mock_post:
            execute_tool("GITHUB_LIST_PRS", {}, auth_env, config, "test-conn")
            body = mock_post.call_args.kwargs["json"]
            assert body["appName"] == "github"
            assert body["entityId"] == "default"

    def test_execute_400_missing_account(self, auth_env: dict, config: dict):
        error_body = {
            "status": 400,
            "successful": False,
            "error": "App name and entity id must be present",
            "requestId": "req-123",
        }
        with patch("httpx.post", return_value=_mock_response(400, error_body)):
            with pytest.raises(ConnectorProviderError) as exc_info:
                execute_tool("GITHUB_LIST_PRS", {}, auth_env, config, "test-conn")
            assert exc_info.value.status_code == 400
            assert exc_info.value.request_id == "req-123"

    def test_execute_404_unknown_action(self, auth_env: dict, config: dict):
        with patch("httpx.post", return_value=_mock_response(404, {"error": "Action not found"})):
            with pytest.raises(ConnectorProviderError) as exc_info:
                execute_tool("NONEXISTENT_ACTION", {}, auth_env, config, "test-conn")
            assert exc_info.value.status_code == 404

    def test_execute_timeout(self, auth_env: dict, config: dict):
        with patch("httpx.post", side_effect=httpx.ReadTimeout("timeout")):
            with pytest.raises(ConnectorProviderError, match="Timeout"):
                execute_tool("TEST", {}, auth_env, config, "test-conn")

    def test_execute_http_error(self, auth_env: dict, config: dict):
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(ConnectorProviderError, match="HTTP error"):
                execute_tool("TEST", {}, auth_env, config, "test-conn")

    def test_execute_url_uses_slug(self, auth_env: dict, config: dict):
        mock_data = {"successful": True, "data": {}}
        with patch("httpx.post", return_value=_mock_response(200, mock_data)) as mock_post:
            execute_tool("MY_CUSTOM_TOOL", {}, auth_env, config, "test-conn")
            url = mock_post.call_args.args[0]
            assert url.endswith("/actions/MY_CUSTOM_TOOL/execute")
